/**
 * Health Data Upload Server
 *
 * Accepts CSV file uploads and triggers health data imports.
 * Designed to run behind Tailscale Serve for secure phone → computer uploads.
 *
 * Routes:
 *   POST /upload  — Save CSV to import folder (fast, no processing)
 *   POST /sync    — Run daily_import.py on all pending files
 *   GET  /status  — Health check
 *
 * Configuration (env vars):
 *   HEALTH_UPLOAD_PORT    — Server port (default: 8766)
 *   HEALTH_UPLOAD_TOKEN   — Bearer token for auth (required)
 *   HEALTH_UPLOAD_DIR     — Where to save CSVs (default: from config.yaml icloud_folder)
 *   HEALTH_IMPORT_SCRIPT  — Script to run on sync (default: ./trigger-import.sh)
 *
 * Usage:
 *   HEALTH_UPLOAD_TOKEN=secret bun run server/upload-server.ts
 */

import { $ } from "bun";

const PORT = parseInt(process.env.HEALTH_UPLOAD_PORT || "8766");
const TOKEN = process.env.HEALTH_UPLOAD_TOKEN || "";
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB
const ALLOWED_EXTENSIONS = [".csv"];

const REPO_ROOT = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const IMPORT_SCRIPT =
  process.env.HEALTH_IMPORT_SCRIPT || `${REPO_ROOT}/server/trigger-import.sh`;

if (!TOKEN) {
  console.error("HEALTH_UPLOAD_TOKEN is required");
  process.exit(1);
}

// Resolve upload directory: env var > config.yaml > error
async function resolveUploadDir(): Promise<string> {
  if (process.env.HEALTH_UPLOAD_DIR) {
    return process.env.HEALTH_UPLOAD_DIR;
  }

  try {
    const result = await $`bash ${REPO_ROOT}/shell/paths.sh`.text();
    const match = result.match(/^icloud=(.+)$/m);
    if (match) return match[1];
  } catch {}

  console.error(
    "Could not resolve upload directory. Set HEALTH_UPLOAD_DIR or configure icloud_folder in config.yaml"
  );
  process.exit(1);
}

const UPLOAD_DIR = await resolveUploadDir();

// Track sync state
let syncRunning = false;

async function runSync(): Promise<{ ok: boolean; output: string }> {
  if (syncRunning) {
    return { ok: false, output: "Sync already in progress" };
  }

  syncRunning = true;
  try {
    const proc = Bun.spawn(["bash", IMPORT_SCRIPT], {
      cwd: REPO_ROOT,
      stdout: "pipe",
      stderr: "pipe",
    });

    const stdout = await new Response(proc.stdout).text();
    const stderr = await new Response(proc.stderr).text();
    const exitCode = await proc.exited;

    const output = (stdout + "\n" + stderr).trim();
    console.log(
      `[${new Date().toISOString()}] Sync completed (exit ${exitCode})`
    );

    return { ok: exitCode === 0, output };
  } catch (e) {
    return { ok: false, output: `Sync error: ${e}` };
  } finally {
    syncRunning = false;
  }
}

function checkAuth(req: Request): Response | null {
  const auth = req.headers.get("Authorization");
  if (!auth || auth !== `Bearer ${TOKEN}`) {
    return Response.json({ ok: false, error: "Unauthorized" }, { status: 401 });
  }
  return null;
}

const server = Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",

  async fetch(req) {
    const url = new URL(req.url);
    // Strip path prefix from reverse proxy (e.g. Tailscale Serve /health/)
    const path = url.pathname.replace(/^\/health/, "") || "/";

    // Log all incoming requests for debugging
    console.log(
      `[${new Date().toISOString()}] ${req.method} ${url.pathname} Content-Type: ${req.headers.get("Content-Type") || "none"}`
    );

    // Health check
    if ((path === "/status" || path === "/") && req.method === "GET") {
      return Response.json({
        ok: true,
        service: "health-upload",
        uploadDir: UPLOAD_DIR,
        syncRunning,
      });
    }

    // Upload endpoint — save file only, no import
    if (path === "/upload" && req.method === "POST") {
      const authErr = checkAuth(req);
      if (authErr) return authErr;

      try {
        let formData: FormData;
        try {
          formData = await req.formData();
        } catch {
          return Response.json(
            {
              ok: false,
              error:
                "No file provided. Send multipart/form-data with a 'file' field.",
            },
            { status: 400 }
          );
        }

        // Log all form field names for debugging
        const fieldNames: string[] = [];
        formData.forEach((value, key) => {
          const info = value instanceof File
            ? `File(${value.name}, ${value.size}B, ${value.type})`
            : `String(${String(value).substring(0, 100)})`;
          fieldNames.push(`${key}: ${info}`);
        });
        console.log(`[${new Date().toISOString()}] Form fields: ${fieldNames.join(", ") || "none"}`);

        // Accept file from any field name (Health Auto Export may use different names)
        let file: File | null = null;
        let fieldKey = "";
        formData.forEach((value, key) => {
          if (value instanceof File && !file) {
            file = value;
            fieldKey = key;
          }
        });

        if (!file) {
          return Response.json(
            { ok: false, error: "No file provided" },
            { status: 400 }
          );
        }

        // Validate extension
        const ext = file.name
          .substring(file.name.lastIndexOf("."))
          .toLowerCase();
        if (!ALLOWED_EXTENSIONS.includes(ext)) {
          return Response.json(
            {
              ok: false,
              error: `Invalid file type: ${ext}. Allowed: ${ALLOWED_EXTENSIONS.join(", ")}`,
            },
            { status: 400 }
          );
        }

        // Validate size
        if (file.size > MAX_FILE_SIZE) {
          return Response.json(
            {
              ok: false,
              error: `File too large: ${(file.size / 1024 / 1024).toFixed(1)}MB. Max: ${MAX_FILE_SIZE / 1024 / 1024}MB`,
            },
            { status: 400 }
          );
        }

        // Clean up filename — Health Auto Export sends full iOS paths as filenames
        let cleanName = (file as File).name;
        // Strip file:// prefix and path, keep just the filename
        if (cleanName.includes("/")) {
          cleanName = cleanName.split("/").pop() || cleanName;
        }

        // Map Health Auto Export field names to daily_import.py expected prefixes
        // Map Health Auto Export field names → daily_import.py expected prefixes
        // Unknown types are saved as-is — daily_import.py can add support later
        const prefixMap: Record<string, string> = {
          HealthData: "HealthMetrics",
          MedicationsData: "Medications",
          WorkoutsData: "Workouts",
          CycleTrackingData: "CycleTracking",
          SymptomsData: "Symptoms",
          ECGData: "ECG",
          StateOfMindData: "StateOfMind",
          HeartRateNotificationsData: "HeartRateNotifications",
        };

        // Generate a clean filename: Prefix-YYYY-MM-DD.csv
        const today = new Date()
          .toLocaleDateString("en-CA") // YYYY-MM-DD format
          .replace(/\//g, "-");
        const prefix = prefixMap[fieldKey] || fieldKey;
        cleanName = `${prefix}-${today}.csv`;

        // Save to upload directory
        const savePath = `${UPLOAD_DIR}/${cleanName}`;
        await Bun.write(savePath, await file.arrayBuffer());

        console.log(
          `[${new Date().toISOString()}] Saved: ${cleanName} (${(file.size / 1024).toFixed(0)}KB) → ${savePath}`
        );

        return Response.json({
          ok: true,
          filename: cleanName,
          size: file.size,
          status: "uploaded",
        });
      } catch (e) {
        console.error(`[${new Date().toISOString()}] Upload error:`, e);
        return Response.json(
          { ok: false, error: `Server error: ${e}` },
          { status: 500 }
        );
      }
    }

    // Sync endpoint — run import pipeline
    if (path === "/sync" && req.method === "POST") {
      const authErr = checkAuth(req);
      if (authErr) return authErr;

      console.log(`[${new Date().toISOString()}] Sync requested`);
      const result = await runSync();

      // Extract summary from output
      const lines = result.output.split("\n");
      const summaryLine = lines.find((l) => l.includes("IMPORT SUMMARY"));
      const newFiles =
        lines.find((l) => l.includes("New files found"))?.trim() || "";
      const imported =
        lines.find((l) => l.includes("Successfully imported"))?.trim() || "";
      const errors = lines.find((l) => l.includes("Errors:"))?.trim() || "";

      return Response.json({
        ok: result.ok,
        status: result.ok ? "synced" : "error",
        summary: { newFiles, imported, errors },
        output: result.output.substring(0, 2000),
      });
    }

    return Response.json({ ok: false, error: "Not found" }, { status: 404 });
  },
});

console.log(`🏥 Health Upload server listening on http://127.0.0.1:${PORT}`);
console.log(`   Upload: POST /upload  (save file only)`);
console.log(`   Sync:   POST /sync    (run import pipeline)`);
console.log(`   Status: GET  /status`);
console.log(`   Upload dir: ${UPLOAD_DIR}`);
