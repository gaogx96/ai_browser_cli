/**
 * Agent Browser Client — TypeScript/Node.js SDK for agent-browser-cli v0.5+.
 *
 * Supports two modes:
 *   1. Launch mode: spawns a new Chrome instance.
 *   2. Connect mode: attaches to an existing Chrome via --remote-debugging-port.
 *
 * Usage (launch):
 *   const client = new BrowserClient();
 *   await client.start();
 *   const result = await client.navigate("https://example.com");
 *   console.log(result.tree);
 *   await client.close();
 *
 * Usage (connect — reuses human login sessions):
 *   const client = new BrowserClient({ connect: "http://127.0.0.1:9222" });
 *   await client.start();
 *   const result = await client.navigate("https://www.github.com");
 *   await client.close();
 *
 * Usage (with statement-like pattern):
 *   await using client = await BrowserClient.create();
 *   await client.navigate("https://example.com");
 */

import { spawn, ChildProcess } from "node:child_process";
import { createInterface, type Interface } from "node:readline";
import { existsSync } from "node:fs";
import { join } from "node:path";

// ─── Types ──────────────────────────────────────────────────────────────

export interface CommandResponse {
  status: "ok" | "error" | "ready";
  action?: string;
  error?: string;
  tree?: string;
  title?: string;
  url?: string;
  interactive_count?: number;
  scrolled?: boolean;
  path?: string;
  meta?: Record<string, unknown>;
  message?: string;
  media_enabled?: boolean;
}

export interface BrowserClientOptions {
  /** Path to agent-browser-cli binary. Auto-detected if omitted. */
  executable?: string;
  /** Chrome debugging URL (e.g. "http://127.0.0.1:9222"). Enables connect mode. */
  connect?: string;
  /** Chrome user-data-dir for session reuse. */
  profile?: string;
  /** Block images/CSS/fonts/ads. Default: true. */
  blockResources?: boolean;
  /** Start with media loading enabled. Default: false. */
  mediaEnabled?: boolean;
  /** Show browser window. Default: false. */
  show?: boolean;
  /** Default per-command timeout in ms. Default: 60000. */
  commandTimeoutMs?: number;
}

// ─── Error class ────────────────────────────────────────────────────────

export class BrowserClientError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BrowserClientError";
  }
}

// ─── Client ─────────────────────────────────────────────────────────────

export class BrowserClient {
  private static readonly DEFAULT_TIMEOUT_MS = 60_000;
  private static readonly READY_TIMEOUT_MS = 30_000;

  private readonly executable: string;
  private readonly connect?: string;
  private readonly profile?: string;
  private readonly blockResources: boolean;
  private readonly mediaEnabled: boolean;
  private readonly show: boolean;
  private readonly commandTimeoutMs: number;

  private proc: ChildProcess | null = null;
  private ready = false;
  private rl: Interface | null = null;

  // Response dispatch: FIFO queue + waiter chain
  private responseQueue: CommandResponse[] = [];
  private waiters: Array<(resp: CommandResponse) => void> = [];
  private stderrBuffer: string[] = [];

  constructor(options?: BrowserClientOptions) {
    this.executable = options?.executable ?? BrowserClient.findExecutable();
    this.connect = options?.connect;
    this.profile = options?.profile;
    this.blockResources = options?.blockResources ?? true;
    this.mediaEnabled = options?.mediaEnabled ?? false;
    this.show = options?.show ?? false;
    this.commandTimeoutMs =
      options?.commandTimeoutMs ?? BrowserClient.DEFAULT_TIMEOUT_MS;
  }

  /** Convenience factory: create + start in one call. */
  static async create(
    options?: BrowserClientOptions
  ): Promise<BrowserClient> {
    const client = new BrowserClient(options);
    await client.start();
    return client;
  }

  // ── Lifecycle ──────────────────────────────────────────────────────

  async start(): Promise<void> {
    if (this.proc !== null) {
      throw new BrowserClientError("Client already started");
    }

    const args = ["listen"];

    if (this.connect) args.push("--connect", this.connect);
    if (this.profile) args.push("--profile", this.profile);
    if (!this.blockResources) args.push("--block-resources", "false");
    if (this.mediaEnabled) args.push("--media-enabled");
    if (this.show) args.push("--show");

    this.proc = spawn(this.executable, args, {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });

    // Wire up stdout → response dispatch
    this.rl = createInterface({ input: this.proc.stdout!, crlfDelay: Infinity });
    this.rl.on("line", (line: string) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      try {
        const resp: CommandResponse = JSON.parse(trimmed);
        this.dispatch(resp);
      } catch {
        // Non-JSON line — ignore silently
      }
    });

    // Capture stderr
    this.proc.stderr?.on("data", (chunk: Buffer) => {
      const msg = chunk.toString().trim();
      if (msg) {
        this.stderrBuffer.push(msg);
        if (this.stderrBuffer.length > 500) this.stderrBuffer.shift();
      }
    });

    // Process exit handler — wake ALL waiters before nulling
    this.proc.on("exit", (code, signal) => {
      this.ready = false;
      const err: CommandResponse = {
        status: "error",
        error: `CLI exited (code=${code}, signal=${signal})`,
      };
      // Wake every pending waiter
      while (this.waiters.length > 0) {
        this.waiters.shift()!(err);
      }
      this.responseQueue.length = 0;
      this.proc = null;
    });

    // Register OS-level cleanup: if THIS Node process exits, kill the child
    const cleanup = () => {
      try {
        this.proc?.kill("SIGKILL");
      } catch {
        /* already dead */
      }
    };
    process.on("exit", cleanup);
    process.on("SIGINT", () => {
      cleanup();
      process.exit(130);
    });
    process.on("SIGTERM", () => {
      cleanup();
      process.exit(143);
    });

    // Wait for ready signal
    const readyResp = await this.waitForResponseWithTimeout(
      BrowserClient.READY_TIMEOUT_MS
    );
    if (readyResp.status !== "ready") {
      throw new BrowserClientError(
        `Unexpected ready signal: ${JSON.stringify(readyResp)}`
      );
    }
    this.ready = true;
  }

  async close(): Promise<void> {
    if (this.proc === null) return;
    this.ready = false;

    // Close stdin → EOF → CLI exits listen loop
    try {
      this.proc.stdin?.end();
    } catch {
      /* ignore */
    }

    await this.waitForExit(5_000);

    this.rl?.close();
    this.rl = null;
    this.proc = null;
  }

  /** Symbol.dispose support for `await using` syntax. */
  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }

  // ── Command interface ──────────────────────────────────────────────

  async sendCommand(
    action: string,
    params?: Record<string, unknown>,
    timeoutMs?: number
  ): Promise<CommandResponse> {
    if (!this.ready || this.proc === null) {
      throw new BrowserClientError("Client not started. Call start() first.");
    }

    const command = { action, ...params };
    const payload = JSON.stringify(command) + "\n";

    // Write to stdin
    await new Promise<void>((resolve, reject) => {
      this.proc!.stdin!.write(payload, (err) => {
        if (err) reject(new BrowserClientError(`Stdin write failed: ${err.message}`));
        else resolve();
      });
    });

    // Wait for response
    const effectiveTimeout = timeoutMs ?? this.commandTimeoutMs;
    let resp: CommandResponse;
    try {
      resp = await this.waitForResponseWithTimeout(effectiveTimeout);
    } catch {
      // Emergency screenshot on timeout
      await this.tryEmergencyScreenshot();
      throw new BrowserClientError(
        `Command '${action}' timed out after ${effectiveTimeout}ms`
      );
    }

    if (resp.status === "error") {
      throw new BrowserClientError(resp.error ?? "Unknown CLI error");
    }

    return resp;
  }

  // ── Convenience methods ────────────────────────────────────────────

  async navigate(url: string, timeoutMs?: number): Promise<CommandResponse> {
    return this.sendCommand("navigate", { url }, timeoutMs);
  }

  async click(targetId: string, timeoutMs?: number): Promise<CommandResponse> {
    return this.sendCommand("click", { target_id: targetId }, timeoutMs);
  }

  async typeText(
    targetId: string,
    text: string,
    timeoutMs?: number
  ): Promise<CommandResponse> {
    return this.sendCommand("type", { target_id: targetId, text }, timeoutMs);
  }

  async screenshot(timeoutMs?: number): Promise<string> {
    const resp = await this.sendCommand("screenshot", undefined, timeoutMs);
    return resp.path ?? "";
  }

  async tree(timeoutMs?: number): Promise<string> {
    const resp = await this.sendCommand("tree", undefined, timeoutMs);
    return resp.tree ?? "";
  }

  async meta(
    timeoutMs?: number
  ): Promise<Record<string, unknown>> {
    const resp = await this.sendCommand("meta", undefined, timeoutMs);
    return resp.meta ?? {};
  }

  async configure(
    mediaEnabled: boolean,
    timeoutMs?: number
  ): Promise<CommandResponse> {
    return this.sendCommand(
      "configure",
      { media_enabled: mediaEnabled },
      timeoutMs
    );
  }

  async safeScreenshot(): Promise<string | null> {
    try {
      return await this.screenshot();
    } catch {
      return null;
    }
  }

  get stderrLog(): readonly string[] {
    return [...this.stderrBuffer];
  }

  // ── Internal ───────────────────────────────────────────────────────

  private dispatch(resp: CommandResponse): void {
    if (this.waiters.length > 0) {
      this.waiters.shift()!(resp);
    } else {
      this.responseQueue.push(resp);
    }
  }

  private waitForResponse(): Promise<CommandResponse> {
    if (this.responseQueue.length > 0) {
      return Promise.resolve(this.responseQueue.shift()!);
    }
    return new Promise<CommandResponse>((resolve) => {
      this.waiters.push(resolve);
    });
  }

  private waitForResponseWithTimeout(
    timeoutMs: number
  ): Promise<CommandResponse> {
    return Promise.race([
      this.waitForResponse(),
      new Promise<CommandResponse>((_, reject) =>
        setTimeout(
          () => reject(new BrowserClientError(`Timed out after ${timeoutMs}ms`)),
          timeoutMs
        )
      ),
    ]);
  }

  private async waitForExit(timeoutMs: number): Promise<void> {
    if (this.proc === null) return;
    return new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        try {
          this.proc?.kill("SIGKILL");
        } catch {
          /* already dead */
        }
        resolve();
      }, timeoutMs);

      this.proc?.on("exit", () => {
        clearTimeout(timer);
        resolve();
      });

      // Already exited between check and listener
      if (this.proc === null) {
        clearTimeout(timer);
        resolve();
      }
    });
  }

  private async tryEmergencyScreenshot(): Promise<void> {
    try {
      if (this.proc?.stdin && !this.proc.stdin.destroyed) {
        this.proc.stdin.write(
          JSON.stringify({ action: "screenshot" }) + "\n"
        );
      }
    } catch {
      /* best effort */
    }
  }

  private static findExecutable(): string {
    const isWin = process.platform === "win32";
    const ext = isWin ? ".exe" : "";
    const name = `agent-browser-cli${ext}`;

    const candidates = [
      join("target", "release", name),
      join("target", "debug", name),
      name,
    ];

    for (const c of candidates) {
      if (existsSync(c)) return c;
    }

    return name;
  }
}

// ── Quick test ──────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const url = process.argv[2] ?? "https://www.baidu.com";
  const connect = process.argv[3] ?? undefined;

  console.log(`[*] Starting browser client (connect=${connect ?? "none"})...`);
  const client = new BrowserClient({ connect });

  try {
    await client.start();

    console.log(`[*] Navigating to ${url}...`);
    const result = await client.navigate(url);
    console.log(`[*] Title: ${result.title ?? "N/A"}`);
    console.log(`[*] Elements: ${result.interactive_count ?? 0}`);
    const tree = result.tree ?? "";
    const lines = tree.split("\n");
    for (const line of lines.slice(0, 20)) {
      console.log(`    ${line}`);
    }
    if (lines.length > 20) {
      console.log(`    ... (${lines.length} lines total)`);
    }

    const p = await client.safeScreenshot();
    if (p) console.log(`[*] Screenshot: ${p}`);
  } finally {
    await client.close();
    console.log("[*] Done.");
  }
}

// ESM-compatible entry point check
const isMainModule =
  typeof require !== "undefined" && require.main === module;

if (isMainModule) {
  main().catch((err) => {
    console.error("[!] Error:", err.message);
    process.exit(1);
  });
}
