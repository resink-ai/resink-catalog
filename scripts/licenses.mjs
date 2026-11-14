// Licences of the packages that end up in the web bundle: production dependencies and their dependencies.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..", "web");
const read = (name) => JSON.parse(readFileSync(join(web, "node_modules", name, "package.json"), "utf8"));
const seen = new Map();
const walk = (name) => {
  if (seen.has(name)) return;
  const pkg = read(name);
  seen.set(name, pkg);
  for (const dep of Object.keys(pkg.dependencies ?? {})) walk(dep);
};
for (const dep of Object.keys(JSON.parse(readFileSync(join(web, "package.json"), "utf8")).dependencies)) walk(dep);
for (const [name, pkg] of [...seen].sort()) console.log(`${name.padEnd(24)} ${pkg.version.padEnd(10)} ${pkg.license}`);
