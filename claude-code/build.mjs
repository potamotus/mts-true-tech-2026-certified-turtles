import { build } from "esbuild";
import { readFileSync, existsSync } from "fs";
import { resolve, dirname } from "path";

// Plugin: resolve .js imports to .ts/.tsx files
const resolveJsToTs = {
  name: "resolve-js-to-ts",
  setup(build) {
    build.onResolve({ filter: /\.js$/ }, (args) => {
      // Only resolve relative imports from our src/ directory
      if (!args.path.startsWith(".")) return;
      if (args.resolveDir.includes("node_modules")) return;

      const dir = args.resolveDir;
      const base = args.path.replace(/\.js$/, "");

      for (const ext of [".ts", ".tsx", "/index.ts", "/index.tsx"]) {
        const full = resolve(dir, base + ext);
        if (existsSync(full)) {
          return { path: full };
        }
      }

      // File doesn't exist — stub it
      return { path: args.path, namespace: "stub" };
    });

    build.onLoad({ filter: /.*/, namespace: "stub" }, () => {
      return {
        contents: `
          const noop = () => false;
          const handler = { get: () => noop };
          const stub = new Proxy(noop, handler);
          export default stub;
          export const gate = noop;
          export const isConnectorTextBlock = noop;
          export const WORKFLOW_TOOL_NAME = "workflow";
          export const TungstenTool = class {};
          export const resetLimits = noop;
          export const resetLimitsNonInteractive = noop;
          export const DEFAULT_UPLOAD_CONCURRENCY = 5;
          export const FILE_COUNT_LIMIT = 100;
          export const OUTPUTS_SUBDIR = "outputs";
        `,
        loader: "ts",
      };
    });
  },
};

// Plugin: stub missing/unavailable modules
const stubMissing = {
  name: "stub-missing",
  setup(build) {
    const stubs = [
      "bun:bundle",
      "react/compiler-runtime",
      "@ant/claude-for-chrome-mcp",
      "@ant/computer-use-mcp",
      "@ant/computer-use-swift",
      "@anthropic-ai/claude-agent-sdk",
      "@anthropic-ai/mcpb",
      "@anthropic-ai/bedrock-sdk",
      "@anthropic-ai/foundry-sdk",
      "@anthropic-ai/sandbox-runtime",
      "@anthropic-ai/vertex-sdk",
      "@aws-sdk/client-bedrock-runtime",
      "@aws-sdk/client-bedrock",
      "@aws-sdk/client-sts",
      "@aws-sdk/credential-provider-node",
      "@azure/identity",
      "@smithy/core",
      "@smithy/node-http-handler",
      "google-auth-library",
      "fflate",
      "@ant/computer-use-input",
      "@opentelemetry/exporter-logs-otlp-grpc",
      "@opentelemetry/exporter-logs-otlp-http",
      "@opentelemetry/exporter-logs-otlp-proto",
      "@opentelemetry/exporter-metrics-otlp-grpc",
      "@opentelemetry/exporter-metrics-otlp-http",
      "@opentelemetry/exporter-metrics-otlp-proto",
      "@opentelemetry/exporter-prometheus",
      "@opentelemetry/exporter-trace-otlp-grpc",
      "@opentelemetry/exporter-trace-otlp-http",
      "@opentelemetry/exporter-trace-otlp-proto",
      "audio-capture-napi",
      "color-diff-napi",
      "modifiers-napi",
      "sharp",
      "turndown",
    ];

    for (const mod of stubs) {
      build.onResolve({ filter: new RegExp(`^${mod.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) }, () => {
        return { path: mod, namespace: "stub-mod" };
      });
    }

    build.onLoad({ filter: /.*/, namespace: "stub-mod" }, (args) => {
      return {
        contents: `
          const noop = (...a) => false;
          const handler = { get: (t, p) => p === '__esModule' ? true : noop };
          const stub = new Proxy(noop, handler);
          export default stub;
          export const feature = noop;
          export const gate = noop;
          export const c = () => [];
          export const Anthropic = class { constructor() {} messages = { create: async () => ({}) } };
          export const SandboxManager = class {};
          export const createComputerUseMcpServer = noop;
          export const buildComputerUseTools = noop;
          export const bindSessionContext = noop;
          export const getSentinelCategory = noop;
          export const targetImageSize = noop;
          export const API_RESIZE_PARAMS = {};
          export const DEFAULT_GRANT_FLAGS = {};
          export const createClaudeForChromeMcpServer = noop;
          export const ColorDiff = class {};
          export const ColorFile = class {};
          export const getSyntaxTheme = noop;
          export const BROWSER_TOOLS = [];
        `,
        loader: "ts",
      };
    });
  },
};

// Plugin: resolve bare "src/..." imports to relative paths
const resolveSrcPaths = {
  name: "resolve-src-paths",
  setup(build) {
    build.onResolve({ filter: /^src\// }, (args) => {
      const rel = args.path.replace(/^src\//, "./");
      const base = rel.replace(/\.js$/, "");
      const dir = resolve("src");
      for (const ext of [".ts", ".tsx", "/index.ts", "/index.tsx", ".js"]) {
        const full = resolve(dir, base.replace(/^\.\//, "") + ext);
        if (existsSync(full)) return { path: full };
      }
      return { path: args.path, namespace: "stub" };
    });

    // Resolve global.d.ts
    build.onResolve({ filter: /global\.d\.ts$/ }, () => {
      return { path: "global.d.ts", namespace: "stub" };
    });

    // Load .md and .txt files as text
    build.onResolve({ filter: /\.(md|txt)$/ }, (args) => {
      if (!args.path.startsWith(".")) return;
      const full = resolve(args.resolveDir, args.path);
      if (existsSync(full)) return { path: full, namespace: "md" };
      return { path: full, namespace: "md-stub" };
    });
    build.onResolve({ filter: /\.txt$/ }, (args) => {
      if (!args.path.startsWith(".")) return;
      const full = resolve(args.resolveDir, args.path);
      if (existsSync(full)) return { path: full, namespace: "md" };
      return { path: full, namespace: "md-stub" };
    });
    build.onLoad({ filter: /.*/, namespace: "md" }, (args) => {
      const text = readFileSync(args.path, "utf-8");
      return { contents: `export default ${JSON.stringify(text)};`, loader: "ts" };
    });
    build.onLoad({ filter: /.*/, namespace: "md-stub" }, () => {
      return { contents: `export default "";`, loader: "ts" };
    });
  },
};

try {
  await build({
    entryPoints: ["src/main.tsx"],
    bundle: true,
    platform: "node",
    target: "node22",
    format: "esm",
    outfile: "dist/main.js",
    jsx: "automatic",
    loader: {
      ".ts": "ts",
      ".tsx": "tsx",
    },
    external: ["fsevents"],
    plugins: [stubMissing, resolveJsToTs, resolveSrcPaths],
    logLevel: "warning",
    logLimit: 20,
    define: {
      "process.env.NODE_ENV": '"production"',
    },
    banner: {
      js: `import { createRequire as __cr } from 'module'; const require = __cr(import.meta.url);`,
    },
  });
  console.log("Build OK → dist/main.js");
} catch (e) {
  process.exit(1);
}
