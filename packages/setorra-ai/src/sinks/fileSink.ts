import { mkdirSync, writeFileSync, appendFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";

/**
 * NDJSON file sink for offline-first persistence.
 *
 * Privacy: this sink expects payloads are already redacted. Secrets must
 * never be written. This class does not attempt to redact.
 */
export class FileSink {
  private readonly filePath: string;

  constructor(baseDir: string, fileName = "events.ndjson") {
    const dir = baseDir || ".";
    this.filePath = join(dir, fileName);
    const parent = dirname(this.filePath);
    if (!existsSync(parent)) {
      mkdirSync(parent, { recursive: true });
    }
    if (!existsSync(this.filePath)) {
      writeFileSync(this.filePath, "", { encoding: "utf-8" });
    }
  }

  async append(line: string): Promise<void> {
    appendFileSync(this.filePath, line + "\n", { encoding: "utf-8" });
  }

  path(): string {
    return this.filePath;
  }

  /** Atomically write a JSON file in the same base directory. */
  static writeJsonAtomic(absPath: string, obj: unknown): void {
    const dir = dirname(absPath);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    const tmp = join(dir, `.tmp.${Date.now()}.${Math.random().toString(36).slice(2)}.json`);
    writeFileSync(tmp, JSON.stringify(obj, null, 2), { encoding: "utf-8" });
    // Best-effort atomic rename on same filesystem
    try {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const fs = require("node:fs") as typeof import("node:fs");
      fs.renameSync(tmp, absPath);
    } catch {
      writeFileSync(absPath, JSON.stringify(obj, null, 2), { encoding: "utf-8" });
      try {
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        const fs = require("node:fs") as typeof import("node:fs");
        fs.unlinkSync(tmp);
      } catch { /* ignore */ }
    }
  }
}
