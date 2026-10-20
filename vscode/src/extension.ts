import { ChildProcess, spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

const SOURCE = "py-extended-types";
const SAVE_DEBOUNCE_MS = 250;
const TYPE_DEBOUNCE_MS = 400;
const RUN_TIMEOUT_MS = 60_000;

/** Positions are 1-based, `endColumn` exclusive, as in the CLI's JSON. */
interface ReportEdit {
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
  newText: string;
}

interface ReportFix {
  title: string;
  edits: ReportEdit[];
}

/** One entry of `py-extended-types check --format json`. Positions are 1-based, `endColumn` exclusive. */
interface ReportDiagnostic {
  path: string;
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
  severity: "error";
  rule: string;
  message: string;
  fix?: ReportFix | null;
  /** For import errors: the file the exception was raised in. */
  origin?: string | null;
}

interface Report {
  version: number;
  classes: number;
  files: number;
  diagnostics: ReportDiagnostic[];
}

type RunState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; problems: number; classes: number }
  | { kind: "failed"; reason: string };

let output: vscode.OutputChannel;
let collection: vscode.DiagnosticCollection;
let statusBar: vscode.StatusBarItem;
const checkers = new Map<string, FolderChecker>();
/**
 * Fixes for the diagnostics currently published, keyed by file URI, with the document version
 * they were computed against (undefined when the file wasn't open, i.e. it was read from disk).
 */
const fixesByFile = new Map<
  string,
  { version: number | undefined; fixes: Array<{ diagnostic: vscode.Diagnostic; fix: ReportFix }> }
>();
let warnedMissingCommand = false;

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("py-extended-types");
  collection = vscode.languages.createDiagnosticCollection(SOURCE);
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 0);
  statusBar.command = "pyExtendedTypes.showOutput";
  context.subscriptions.push(output, collection, statusBar);

  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    addFolder(folder);
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("pyExtendedTypes.check", () => {
      for (const checker of checkers.values()) {
        checker.schedule(0);
      }
    }),
    vscode.commands.registerCommand("pyExtendedTypes.showOutput", () => output.show(true)),
    vscode.commands.registerCommand("pyExtendedTypes.restart", restart),
    vscode.languages.registerCodeActionsProvider(
      { scheme: "file", language: "python" },
      new FixProvider(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] },
    ),
    vscode.workspace.onDidSaveTextDocument((document) => {
      checkerFor(document)?.schedule(SAVE_DEBOUNCE_MS);
    }),
    vscode.workspace.onDidChangeTextDocument((event) => {
      if (event.contentChanges.length === 0) {
        return;
      }
      const checker = checkerFor(event.document);
      if (checker && checker.runMode() === "onType") {
        checker.schedule(TYPE_DEBOUNCE_MS);
      }
    }),
    vscode.workspace.onDidCloseTextDocument((document) => {
      // Closing without saving drops the buffer, so the files on disk are the truth again.
      const checker = checkerFor(document);
      if (checker && checker.runMode() === "onType") {
        checker.schedule(SAVE_DEBOUNCE_MS);
      }
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (!event.affectsConfiguration("pyExtendedTypes")) {
        return;
      }
      for (const checker of checkers.values()) {
        checker.schedule(0);
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders((event) => {
      event.removed.forEach(removeFolder);
      event.added.forEach(addFolder);
    }),
  );
}

export function deactivate(): void {
  for (const checker of checkers.values()) {
    checker.dispose();
  }
  checkers.clear();
}

function isPythonFile(document: vscode.TextDocument): boolean {
  return document.uri.scheme === "file" && (document.languageId === "python" || document.fileName.endsWith(".py"));
}

function checkerFor(document: vscode.TextDocument): FolderChecker | undefined {
  if (!isPythonFile(document)) {
    return undefined;
  }
  const folder = vscode.workspace.getWorkspaceFolder(document.uri);
  return folder ? checkers.get(folder.uri.toString()) : undefined;
}

/** Kill every run, drop all diagnostics, and start over with freshly read settings. */
function restart(): void {
  output.appendLine(`[${new Date().toLocaleTimeString()}] restarting`);
  deactivate();
  collection.clear();
  fixesByFile.clear();
  warnedMissingCommand = false;
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    addFolder(folder);
  }
  renderStatus();
}

function addFolder(folder: vscode.WorkspaceFolder): void {
  const checker = new FolderChecker(folder);
  checkers.set(folder.uri.toString(), checker);
  checker.schedule(0);
}

function removeFolder(folder: vscode.WorkspaceFolder): void {
  const key = folder.uri.toString();
  checkers.get(key)?.dispose();
  checkers.delete(key);
  renderStatus();
}

type RunMode = "onType" | "onSave";

interface Settings {
  enable: boolean;
  command: string[];
  paths: string[];
  run: RunMode;
}

function settings(folder: vscode.WorkspaceFolder): Settings {
  const config = vscode.workspace.getConfiguration("pyExtendedTypes", folder.uri);
  return {
    enable: config.get<boolean>("enable", true),
    command: config.get<string[]>("path", []),
    paths: config.get<string[]>("paths", ["."]),
    run: config.get<RunMode>("run", "onType"),
  };
}

/** Prefer the workspace's own venv so the checker imports the project's dependencies. */
function resolveCommand(folder: vscode.WorkspaceFolder, configured: string[]): string[] {
  if (configured.length > 0) {
    return configured;
  }
  const venvBin = process.platform === "win32"
    ? path.join(folder.uri.fsPath, ".venv", "Scripts", "py-extended-types.exe")
    : path.join(folder.uri.fsPath, ".venv", "bin", "py-extended-types");
  return fs.existsSync(venvBin) ? [venvBin] : ["py-extended-types"];
}

class FolderChecker {
  state: RunState = { kind: "idle" };
  private settledState: RunState = { kind: "idle" };
  private timer: NodeJS.Timeout | undefined;
  private process: ChildProcess | undefined;
  private published = new Set<string>();

  constructor(private readonly folder: vscode.WorkspaceFolder) {}

  runMode(): RunMode {
    return settings(this.folder).run;
  }

  schedule(delayMs: number): void {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.run(), delayMs);
  }

  dispose(): void {
    clearTimeout(this.timer);
    this.cancel();
    this.publish(new Map());
  }

  private cancel(): void {
    // A newer edit or save supersedes the in-flight run; its results would be stale.
    this.process?.kill();
    this.process = undefined;
  }

  private run(): void {
    this.cancel();
    const { enable, command, paths, run } = settings(this.folder);
    if (!enable) {
      this.publish(new Map());
      this.setState({ kind: "idle" });
      return;
    }

    // Snapshot the open documents: in onType mode unsaved ones are checked from their buffers.
    // Every published fix is tied to the version it was computed against; in onSave mode an
    // unsaved document gets no version, so its fixes wait for the next save.
    const versions = new Map<string, number>();
    const unsaved: Record<string, string> = {};
    for (const document of vscode.workspace.textDocuments) {
      if (!isPythonFile(document) || vscode.workspace.getWorkspaceFolder(document.uri) !== this.folder) {
        continue;
      }
      if (!document.isDirty) {
        versions.set(document.uri.toString(), document.version);
      } else if (run === "onType") {
        versions.set(document.uri.toString(), document.version);
        unsaved[document.uri.fsPath] = document.getText();
      }
    }
    const hasUnsaved = Object.keys(unsaved).length > 0;

    const [executable, ...prefix] = resolveCommand(this.folder, command);
    const args = [...prefix, "check", "--format", "json", ...(hasUnsaved ? ["--stdin"] : []), ...paths];
    const cwd = this.folder.uri.fsPath;
    const buffers = hasUnsaved ? ` (${Object.keys(unsaved).length} unsaved buffer(s) on stdin)` : "";
    output.appendLine(`[${new Date().toLocaleTimeString()}] ${cwd}$ ${[executable, ...args].join(" ")}${buffers}`);
    this.setState({ kind: "running" });

    const child = spawn(executable, args, { cwd, env: process.env });
    this.process = child;
    // The process may be killed by a newer run before it reads stdin.
    child.stdin?.on("error", () => undefined);
    child.stdin?.end(hasUnsaved ? JSON.stringify(unsaved) : "");
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => child.kill(), RUN_TIMEOUT_MS);
    child.stdout?.on("data", (chunk: Buffer) => (stdout += chunk.toString()));
    child.stderr?.on("data", (chunk: Buffer) => (stderr += chunk.toString()));

    child.on("error", (error: NodeJS.ErrnoException) => {
      clearTimeout(timeout);
      if (this.process !== child) {
        return;
      }
      this.process = undefined;
      this.fail(error.code === "ENOENT" ? `'${executable}' not found` : error.message);
      if (error.code === "ENOENT" && !warnedMissingCommand) {
        warnedMissingCommand = true;
        void vscode.window.showWarningMessage(
          `py-extended-types: could not run '${executable}'. Install py-extended-types in the workspace venv or set "pyExtendedTypes.path".`,
        );
      }
    });

    child.on("close", (code, signal) => {
      clearTimeout(timeout);
      if (this.process !== child) {
        return;
      }
      this.process = undefined;
      if (stderr.trim()) {
        output.appendLine(stderr.trimEnd());
      }
      if (signal) {
        this.fail(`killed by ${signal}`);
        return;
      }
      // Exit 0 = clean, 1 = problems found; anything else is a crash or usage error.
      if (code !== 0 && code !== 1) {
        this.fail(`exited with code ${code}`);
        return;
      }
      let report: Report;
      try {
        report = JSON.parse(stdout) as Report;
      } catch {
        this.fail("could not parse JSON output");
        output.appendLine(stdout.trimEnd());
        return;
      }
      const unsavedPaths = new Set(Object.keys(unsaved).map((file) => path.resolve(file)));
      const brokenByEdit = report.diagnostics.find(
        (entry) =>
          entry.rule === "py-extended-types(import-error)" &&
          [entry.path, entry.origin].some((file) => file && unsavedPaths.has(path.resolve(file))),
      );
      if (brokenByEdit) {
        // Half-typed code often doesn't import yet; the language server already flags it.
        // Keep the last results rather than flicker until it imports (or is saved). Import
        // errors in files that aren't being edited are real and get published as usual.
        output.appendLine(
          `  unsaved code doesn't import yet (${brokenByEdit.message}); keeping previous results`,
        );
        this.setState(this.settledState);
        return;
      }
      this.publish(toDiagnostics(report.diagnostics), versions);
      output.appendLine(
        `  ${report.diagnostics.length} problem(s) in ${report.classes} class(es) across ${report.files} file(s)`,
      );
      this.setState({ kind: "done", problems: report.diagnostics.length, classes: report.classes });
    });
  }

  private fail(reason: string): void {
    output.appendLine(`  failed: ${reason}`);
    this.setState({ kind: "failed", reason });
  }

  private setState(state: RunState): void {
    this.state = state;
    if (state.kind !== "running") {
      this.settledState = state;
    }
    renderStatus();
  }

  /** Replace this folder's diagnostics, clearing files that no longer have problems. */
  private publish(byFile: Map<string, PublishedEntry[]>, versions = new Map<string, number>()): void {
    for (const key of this.published) {
      if (!byFile.has(key)) {
        collection.delete(vscode.Uri.parse(key));
        fixesByFile.delete(key);
      }
    }
    for (const [key, entries] of byFile) {
      collection.set(vscode.Uri.parse(key), entries.map((entry) => entry.diagnostic));
      fixesByFile.set(key, {
        version: versions.get(key),
        fixes: entries.flatMap((entry) => (entry.fix ? [{ diagnostic: entry.diagnostic, fix: entry.fix }] : [])),
      });
    }
    this.published = new Set(byFile.keys());
  }
}

interface PublishedEntry {
  diagnostic: vscode.Diagnostic;
  fix: ReportFix | null;
}

function toRange(edit: { line: number; column: number; endLine: number; endColumn: number }): vscode.Range {
  return new vscode.Range(edit.line - 1, edit.column - 1, edit.endLine - 1, edit.endColumn - 1);
}

function toDiagnostics(entries: ReportDiagnostic[]): Map<string, PublishedEntry[]> {
  const byFile = new Map<string, PublishedEntry[]>();
  for (const entry of entries) {
    const start = new vscode.Position(entry.line - 1, entry.column - 1);
    let end = new vscode.Position(entry.endLine - 1, entry.endColumn - 1);
    if (!end.isAfter(start)) {
      end = start.translate(0, 1);
    }
    const diagnostic = new vscode.Diagnostic(
      new vscode.Range(start, end),
      entry.message,
      vscode.DiagnosticSeverity.Error,
    );
    diagnostic.source = SOURCE;
    // The CLI reports rules as "py-extended-types(type-mismatch)"; VS Code renders source + code that way itself.
    diagnostic.code = /^py-extended-types\((.+)\)$/.exec(entry.rule)?.[1] ?? entry.rule;
    const key = vscode.Uri.file(entry.path).toString();
    const list = byFile.get(key) ?? [];
    list.push({ diagnostic, fix: entry.fix ?? null });
    byFile.set(key, list);
  }
  return byFile;
}

function renderStatus(): void {
  const states = [...checkers.values()].map((checker) => checker.state);
  if (states.length === 0 || states.every((state) => state.kind === "idle")) {
    statusBar.hide();
    return;
  }
  const failed = states.find((state) => state.kind === "failed");
  const problems = states.reduce((sum, state) => sum + (state.kind === "done" ? state.problems : 0), 0);
  if (states.some((state) => state.kind === "running")) {
    statusBar.text = "$(sync~spin) py-extended-types";
    statusBar.tooltip = "py-extended-types: checking…";
  } else if (failed && failed.kind === "failed") {
    statusBar.text = "$(warning) py-extended-types";
    statusBar.tooltip = `py-extended-types failed: ${failed.reason} (click for output)`;
  } else if (problems > 0) {
    statusBar.text = `$(error) py-extended-types ${problems}`;
    statusBar.tooltip = `py-extended-types: ${problems} problem(s)`;
  } else {
    statusBar.text = "$(check) py-extended-types";
    statusBar.tooltip = "py-extended-types: all shapes match";
  }
  statusBar.show();
}

/**
 * Offers the CLI's fixes as quick fixes (the gutter lightbulb), plus a "fix all" for the file.
 * Fixes are positioned against the document version that was checked, so they're only offered
 * while the document is still at that version; the next check refreshes them.
 */
class FixProvider implements vscode.CodeActionProvider {
  provideCodeActions(
    document: vscode.TextDocument,
    _range: vscode.Range,
    context: vscode.CodeActionContext,
  ): vscode.CodeAction[] {
    const published = fixesByFile.get(document.uri.toString());
    if (!published) {
      return [];
    }
    const current = published.version === undefined ? !document.isDirty : published.version === document.version;
    const available = current ? published.fixes : [];
    if (available.length === 0) {
      return [];
    }

    const actions: vscode.CodeAction[] = [];
    for (const diagnostic of context.diagnostics) {
      if (diagnostic.source !== SOURCE) {
        continue;
      }
      const match = available.find((entry) => sameDiagnostic(entry.diagnostic, diagnostic));
      if (!match) {
        continue;
      }
      const action = new vscode.CodeAction(match.fix.title, vscode.CodeActionKind.QuickFix);
      action.diagnostics = [diagnostic];
      action.isPreferred = true;
      action.edit = workspaceEdit(document.uri, match.fix.edits);
      actions.push(action);
    }

    if (actions.length > 0 && available.length > 1) {
      const fixAll = new vscode.CodeAction(
        "Fix all py-extended-types problems in this file",
        vscode.CodeActionKind.QuickFix,
      );
      fixAll.diagnostics = available.map((entry) => entry.diagnostic);
      fixAll.edit = workspaceEdit(document.uri, combineEdits(available.map((entry) => entry.fix)));
      actions.push(fixAll);
    }
    return actions;
  }
}

function sameDiagnostic(a: vscode.Diagnostic, b: vscode.Diagnostic): boolean {
  return a.range.isEqual(b.range) && a.message === b.message && a.code === b.code;
}

function workspaceEdit(uri: vscode.Uri, edits: ReportEdit[]): vscode.WorkspaceEdit {
  const edit = new vscode.WorkspaceEdit();
  for (const change of edits) {
    edit.replace(uri, toRange(change), change.newText);
  }
  return edit;
}

/**
 * Merge several fixes into one edit list: identical edits (problems on the same field share a
 * rewrite) are applied once, and an edit overlapping one already taken is dropped. Inserts at
 * the same position keep their order, so added fields come out in the shape's order.
 */
function combineEdits(fixes: ReportFix[]): ReportEdit[] {
  const seen = new Set<string>();
  const unique = fixes
    .flatMap((fix) => fix.edits)
    .filter((edit) => {
      const key = JSON.stringify(edit);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });

  const taken: ReportEdit[] = [];
  for (const edit of unique) {
    const range = toRange(edit);
    if (!taken.some((other) => overlaps(toRange(other), range))) {
      taken.push(edit);
    }
  }
  return taken;
}

/** Ranges that merely touch don't conflict, unless an insert would land strictly inside the other. */
function overlaps(a: vscode.Range, b: vscode.Range): boolean {
  const intersection = a.intersection(b);
  if (!intersection) {
    return false;
  }
  if (!intersection.isEmpty) {
    return true;
  }
  const point = intersection.start;
  const inside = (range: vscode.Range) => point.isAfter(range.start) && point.isBefore(range.end);
  return inside(a) || inside(b);
}
