import * as vscode from "vscode";
import * as net from "net";
import * as path from "path";
import * as fs from "fs";

const SOCKET_NAME = ".foreman/extension.sock";

interface ForemanMessage {
  action: "create_terminal" | "kill_terminal" | "send_text";
  name: string;
  command?: string;
  text?: string;
}

class ForemanServer {
  private server: net.Server | null = null;
  private terminals = new Map<string, vscode.Terminal>();
  private socketPath: string;

  constructor(workspaceRoot: string) {
    this.socketPath = path.join(workspaceRoot, SOCKET_NAME);
  }

  start(): void {
    if (this.server) {
      return;
    }

    if (fs.existsSync(this.socketPath)) {
      fs.unlinkSync(this.socketPath);
    }

    this.server = net.createServer((conn) => this.handleConnection(conn));
    this.server.listen(this.socketPath, () => {
      vscode.window.showInformationMessage("Foreman IPC server started");
    });

    this.server.on("error", (err) => {
      vscode.window.showErrorMessage(`Foreman IPC error: ${err.message}`);
    });

    vscode.window.onDidCloseTerminal((closed) => {
      for (const [name, terminal] of this.terminals) {
        if (terminal === closed) {
          this.terminals.delete(name);
          break;
        }
      }
    });
  }

  stop(): void {
    if (this.server) {
      this.server.close();
      this.server = null;
    }

    if (fs.existsSync(this.socketPath)) {
      fs.unlinkSync(this.socketPath);
    }
  }

  private handleConnection(conn: net.Socket): void {
    let buffer = "";

    conn.on("data", (data) => {
      buffer += data.toString();
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const msg: ForemanMessage = JSON.parse(line);
          this.dispatch(msg);
        } catch {
          // Malformed message — ignore
        }
      }
    });
  }

  private dispatch(msg: ForemanMessage): void {
    switch (msg.action) {
      case "create_terminal":
        this.createTerminal(msg.name, msg.command);
        break;
      case "kill_terminal":
        this.killTerminal(msg.name);
        break;
      case "send_text":
        this.sendText(msg.name, msg.text);
        break;
    }
  }

  private createTerminal(name: string, command?: string): void {
    const existing = this.terminals.get(name);
    if (existing) {
      existing.dispose();
      this.terminals.delete(name);
    }

    const terminal = vscode.window.createTerminal({
      name: `Foreman: ${name}`,
      shellPath: "/bin/bash",
      shellArgs: command ? ["-c", command] : undefined,
    });

    this.terminals.set(name, terminal);
    terminal.show(true);
  }

  private killTerminal(name: string): void {
    const terminal = this.terminals.get(name);
    if (terminal) {
      terminal.dispose();
      this.terminals.delete(name);
    }
  }

  private sendText(name: string, text?: string): void {
    const terminal = this.terminals.get(name);
    if (terminal && text) {
      terminal.sendText(text);
    }
  }
}

let server: ForemanServer | null = null;

export function activate(context: vscode.ExtensionContext): void {
  const workspaceFolders = vscode.workspace.workspaceFolders;
  if (!workspaceFolders) {
    return;
  }

  const root = workspaceFolders[0].uri.fsPath;
  server = new ForemanServer(root);

  context.subscriptions.push(
    vscode.commands.registerCommand("foreman.start", () => server?.start()),
    vscode.commands.registerCommand("foreman.stop", () => server?.stop()),
    { dispose: () => server?.stop() },
  );

  const foremanDir = path.join(root, ".foreman");
  if (fs.existsSync(foremanDir)) {
    server.start();
  }
}

export function deactivate(): void {
  server?.stop();
}
