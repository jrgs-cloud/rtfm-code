#!/usr/bin/env node
/**
 * TypeScript type resolver — resolves attribute call chains to their target declarations.
 *
 * Input (stdin): JSON array of {file, line, col, callText}
 * Output (stdout): JSON array of {source, target, edge_type, sourceFile, targetFile}
 *
 * Requires: typescript (peer dep), tsconfig.json in project root.
 */

import * as ts from 'typescript';
import * as path from 'path';
import * as fs from 'fs';

function findTsConfig(startDir) {
  let dir = startDir;
  while (dir !== path.dirname(dir)) {
    const candidate = path.join(dir, 'tsconfig.json');
    if (fs.existsSync(candidate)) return candidate;
    dir = path.dirname(dir);
  }
  return null;
}

function resolveCallSites(callSites, projectRoot) {
  const tsConfigPath = findTsConfig(projectRoot);
  if (!tsConfigPath) {
    process.stderr.write('[ts_resolver] No tsconfig.json found\n');
    return [];
  }

  const configFile = ts.readConfigFile(tsConfigPath, ts.sys.readFile);
  if (configFile.error) {
    process.stderr.write(`[ts_resolver] Error reading tsconfig: ${configFile.error.messageText}\n`);
    return [];
  }

  const parsed = ts.parseJsonConfigFileContent(configFile.config, ts.sys, path.dirname(tsConfigPath));
  const program = ts.createProgram(parsed.fileNames, parsed.options);
  const checker = program.getTypeChecker();

  const results = [];

  for (const site of callSites) {
    try {
      const sourceFile = program.getSourceFile(path.resolve(projectRoot, site.file));
      if (!sourceFile) continue;

      const pos = ts.getPositionOfLineAndCharacter(sourceFile, site.line - 1, site.col);
      const node = findNodeAtPosition(sourceFile, pos);
      if (!node) continue;

      const resolved = resolveNode(node, checker, projectRoot);
      if (resolved) {
        results.push({
          source: site.file + '::' + (site.callText || ''),
          target: resolved.targetFile + '::' + resolved.targetName,
          edge_type: 'type_resolved_call',
          sourceFile: site.file,
          targetFile: resolved.targetFile,
        });
      }
    } catch (e) {
      // Skip unresolvable call sites
    }
  }

  return results;
}

function findNodeAtPosition(sourceFile, pos) {
  function visit(node) {
    if (pos >= node.getStart() && pos < node.getEnd()) {
      const child = ts.forEachChild(node, visit);
      return child || node;
    }
    return undefined;
  }
  return visit(sourceFile);
}

function resolveNode(node, checker, projectRoot) {
  // Walk up to find the call expression or property access
  let current = node;
  while (current && !ts.isCallExpression(current) && !ts.isPropertyAccessExpression(current)) {
    current = current.parent;
  }
  if (!current) return null;

  let symbol;
  if (ts.isCallExpression(current)) {
    symbol = checker.getSymbolAtLocation(current.expression);
  } else if (ts.isPropertyAccessExpression(current)) {
    symbol = checker.getSymbolAtLocation(current.name);
  }

  if (!symbol) return null;

  // Follow aliases
  if (symbol.flags & ts.SymbolFlags.Alias) {
    symbol = checker.getAliasedSymbol(symbol);
  }

  const declarations = symbol.getDeclarations();
  if (!declarations || declarations.length === 0) return null;

  const decl = declarations[0];
  const declFile = decl.getSourceFile().fileName;

  // Skip node_modules and non-project files
  if (declFile.includes('node_modules')) return null;

  let targetFile;
  try {
    targetFile = path.relative(projectRoot, declFile);
  } catch {
    return null;
  }

  // Skip if target is outside project
  if (targetFile.startsWith('..')) return null;

  const targetName = symbol.getName();
  return { targetFile, targetName };
}

// Main
async function main() {
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk;
  }

  let data;
  try {
    data = JSON.parse(input);
  } catch (e) {
    process.stderr.write(`[ts_resolver] Invalid JSON input: ${e.message}\n`);
    process.exit(1);
  }

  const projectRoot = data.projectRoot || process.cwd();
  const callSites = data.callSites || [];

  const results = resolveCallSites(callSites, projectRoot);
  process.stdout.write(JSON.stringify(results));
}

main().catch(e => {
  process.stderr.write(`[ts_resolver] Fatal: ${e.message}\n`);
  process.exit(1);
});
