/**
 * Agent Browser Client - TypeScript/Node.js SDK for agent-browser-cli.
 *
 * Usage:
 *   import { AgentBrowserClient } from 'agent-browser-client';
 *
 *   const client = new AgentBrowserClient();
 *   await client.start();
 *   await client.navigate('https://example.com');
 *   const tree = await client.getTree();
 *   console.log(tree);
 *   await client.stop();
 */

import { spawn, ChildProcess } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';
import * as readline from 'readline';

// ─── Types ──────────────────────────────────────────────────────────────

interface CommandResponse {
  status: 'ok' | 'error' | 'ready';
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

export class AgentBrowserError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AgentBrowserError';
  }
}

// ─── Client ─────────────────────────────────────────────────────────────

export class AgentBrowserClient {
  private static readonly DEFAULT_COMMAND_TIMEOUT_MS = 60_000;

  private executablePath: string;
  private profilePath?: string;
  private blockResources: boolean;
  private commandTimeoutMs: number;

  private process: ChildProcess | null = null;
  private ready: boolean = false;
  private responseQueue: CommandResponse[] = [];
  private responseWaiters: Array<(resp: CommandResponse) => void> = [];
  private stderrBuffer: string[] = [];

  constructor(options?: {
    executablePath?: string;
    profilePath?: string;
    blockResources?: boolean;
    commandTimeoutMs?: number;
  }) {
    this.executablePath = options?.executablePath ?? this.findExecutable();
    this.profilePath = options?.profilePath;
    this.blockResources = options?.blockResources ?? true;
    this.commandTimeoutMs =
      options?.commandTimeoutMs ?? AgentBrowserClient.DEFAULT_COMMAND_TIMEOUT_MS;
  }

  private findExecutable(): string {
    const isWin = process.platform === 'win32';
    const ext = isWin ? '.exe' : '';

    const candidates = [
      path.join('target', 'release', `agent-browser-cli${ext}`),
      path.join('target', 'debug', `agent-browser-cli${ext}`),
      `agent-browser-cli${ext}`,
    ];

    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }

    return `agent-browser-cli${ext}`;
  }

  async start(): Promise<void> {
    if (this.process !== null) {
      throw new AgentBrowserError('Client already started');
    }

    const args = ['listen'];

    if (this.profilePath) {
      args.push('--profile', this.profilePath);
    }

    if (!this.blockResources) {
      args.push('--block-resources', 'false');
    }

    this.process = spawn(this.executablePath, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });

    // Set up stdout line reader
    const rl = readline.createInterface({
      input: this.process.stdout!,
      crlfDelay: Infinity,
    });

    rl.on('line', (line: string) => {
      const trimmed = line.trim();
      if (!trimmed) return;

      try {
        const resp: CommandResponse = JSON.parse(trimmed);
        this.handleResponse(resp);
      } catch {
        console.error(`[agent-browser] Invalid JSON: ${trimmed}`);
      }
    });

    // Collect stderr
    this.process.stderr?.on('data', (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) {
        this.stderrBuffer.push(msg);
        if (this.stderrBuffer.length > 500) {
          this.stderrBuffer.shift();
        }
      }
    });

    // Handle process exit — wake waiters FIRST, then null out process
    this.process.on('exit', (code, signal) => {
      this.ready = false;

      // Wake ALL pending waiters with error BEFORE nulling process
      const errorMsg = `CLI process exited (code=${code}, signal=${signal})`;
      while (this.responseWaiters.length > 0) {
        const waiter = this.responseWaiters.shift()!;
        waiter({ status: 'error', error: errorMsg });
      }

      // Drain queued responses — any remaining are stale
      this.responseQueue.length = 0;

      // NOW safe to null out
      this.process = null;
    });

    // Wait for ready signal with timeout
    const readyResp = await this.waitForResponseWithTimeout(30_000);
    if (readyResp.status !== 'ready') {
      throw new AgentBrowserError(
        `Unexpected ready signal: ${JSON.stringify(readyResp)}`
      );
    }

    this.ready = true;
  }

  async stop(): Promise<void> {
    if (this.process === null) return;

    this.ready = false;

    try {
      this.process.stdin?.end();
    } catch {
      // Ignore
    }

    await new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        try {
          this.process?.kill('SIGKILL');
        } catch {
          // Ignore
        }
        resolve();
      }, 5000);

      this.process?.on('exit', () => {
        clearTimeout(timeout);
        resolve();
      });

      // If process already null (exited between our check and here)
      if (this.process === null) {
        clearTimeout(timeout);
        resolve();
      }
    });

    this.process = null;
  }

  private handleResponse(resp: CommandResponse): void {
    if (this.responseWaiters.length > 0) {
      const waiter = this.responseWaiters.shift()!;
      waiter(resp);
    } else {
      this.responseQueue.push(resp);
    }
  }

  private async waitForResponse(): Promise<CommandResponse> {
    if (this.responseQueue.length > 0) {
      return this.responseQueue.shift()!;
    }

    return new Promise<CommandResponse>((resolve) => {
      this.responseWaiters.push(resolve);
    });
  }

  /**
   * Wait for response with timeout. Rejects if no response within deadline.
   */
  private async waitForResponseWithTimeout(
    timeoutMs: number
  ): Promise<CommandResponse> {
    return Promise.race([
      this.waitForResponse(),
      new Promise<CommandResponse>((_, reject) =>
        setTimeout(
          () => reject(new AgentBrowserError(`Timed out after ${timeoutMs}ms`)),
          timeoutMs
        )
      ),
    ]);
  }

  async sendCommand(command: Record<string, unknown>): Promise<CommandResponse> {
    if (!this.ready || this.process === null) {
      throw new AgentBrowserError('Client not started. Call start() first.');
    }

    const cmdJson = JSON.stringify(command) + '\n';

    // Send the command
    await new Promise<void>((resolve, reject) => {
      this.process!.stdin!.write(cmdJson, (err) => {
        if (err) {
          reject(new AgentBrowserError(`Failed to send command: ${err.message}`));
          return;
        }
        resolve();
      });
    });

    // Wait for response with timeout
    let resp: CommandResponse;
    try {
      resp = await this.waitForResponseWithTimeout(this.commandTimeoutMs);
    } catch {
      throw new AgentBrowserError(
        `Command timed out after ${this.commandTimeoutMs}ms: ${
          (command as Record<string, string>).action ?? '?'
        }`
      );
    }

    if (resp.status === 'error') {
      throw new AgentBrowserError(resp.error ?? 'Unknown error');
    }

    return resp;
  }

  // ─── Convenience Methods ────────────────────────────────────────────

  async navigate(url: string): Promise<CommandResponse> {
    return this.sendCommand({ action: 'navigate', url });
  }

  async click(targetId: string): Promise<CommandResponse> {
    return this.sendCommand({ action: 'click', target_id: targetId });
  }

  async typeText(targetId: string, text: string): Promise<CommandResponse> {
    return this.sendCommand({
      action: 'type',
      target_id: targetId,
      text,
    });
  }

  async screenshot(): Promise<string> {
    const resp = await this.sendCommand({ action: 'screenshot' });
    return resp.path ?? '';
  }

  async getTree(): Promise<string> {
    const resp = await this.sendCommand({ action: 'tree' });
    return resp.tree ?? '';
  }

  async getMeta(): Promise<Record<string, unknown>> {
    const resp = await this.sendCommand({ action: 'meta' });
    return resp.meta ?? {};
  }

  async configure(mediaEnabled: boolean): Promise<CommandResponse> {
    return this.sendCommand({
      action: 'configure',
      media_enabled: mediaEnabled,
    });
  }

  async safeScreenshot(): Promise<string | null> {
    try {
      return await this.screenshot();
    } catch {
      return null;
    }
  }

  get stderrLog(): string[] {
    return [...this.stderrBuffer];
  }
}

// ─── Quick Test ──────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const url = process.argv[2] ?? 'https://example.com';

  console.log('[*] Starting browser client...');
  const client = new AgentBrowserClient();

  try {
    await client.start();

    console.log(`[*] Navigating to ${url}...`);
    const result = await client.navigate(url);
    console.log(`[*] Title: ${result.title ?? 'N/A'}`);
    console.log(`[*] Interactive elements: ${result.interactive_count ?? 0}`);
    console.log(`[*] Tree:\n${result.tree ?? 'empty'}`);

    const p = await client.screenshot();
    console.log(`[*] Screenshot saved: ${p}`);
  } finally {
    await client.stop();
    console.log('[*] Done.');
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error('[!] Error:', err.message);
    process.exit(1);
  });
}
