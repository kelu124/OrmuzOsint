#!/usr/bin/env node
/**
 * push_ais_to_github.js — push new files to kelu124/OrmuzOsint via GitHub REST API.
 *
 * Auth via OneCLI proxy (no PAT needed — proxy injects kelu124 credentials).
 *
 * Usage:
 *   node push_ais_to_github.js                   # auto-detect new ais_data/ files
 *   node push_ais_to_github.js file1 file2 ...   # push specific files
 */

import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const REPO   = 'kelu124/OrmuzOsint';
const BRANCH = 'main';
const API    = 'https://api.github.com';
const REPO_DIR = path.dirname(fileURLToPath(import.meta.url));

const HEADERS = {
  'Authorization': 'token dummy',
  'Accept':        'application/vnd.github+json',
  'Content-Type':  'application/json',
  'User-Agent':    'popeye-nanoclaw',
};

async function get(url) {
  const r = await fetch(url, { headers: HEADERS });
  if (!r.ok) throw new Error(`GET ${url} → ${r.status} ${await r.text()}`);
  return r.json();
}

async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: HEADERS, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`POST ${url} → ${r.status} ${await r.text()}`);
  return r.json();
}

async function patch(url, body) {
  const r = await fetch(url, { method: 'PATCH', headers: HEADERS, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`PATCH ${url} → ${r.status} ${await r.text()}`);
  return r.json();
}

async function main() {
  process.chdir(REPO_DIR);

  // Determine which files to push
  let files = process.argv.slice(2);
  if (files.length === 0) {
    // Auto-detect: untracked files in ais_data/ + modified tracked files there
    const untracked = execSync('git ls-files --others --exclude-standard ais_data/ 2>/dev/null || true')
      .toString().trim().split('\n').filter(Boolean);
    const modified = execSync('git diff --name-only ais_data/ 2>/dev/null || true')
      .toString().trim().split('\n').filter(Boolean);
    files = [...new Set([...untracked, ...modified])];
  }

  if (files.length === 0) {
    console.log('Nothing new in ais_data/ — nothing to push.');
    return;
  }

  // Verify all files exist on disk
  const missing = files.filter(f => !fs.existsSync(f));
  if (missing.length > 0) {
    console.error('Missing files:', missing);
    process.exit(1);
  }

  console.log(`Pushing ${files.length} file(s):`, files);

  // 1. Get current HEAD commit + tree
  const ref     = await get(`${API}/repos/${REPO}/git/refs/heads/${BRANCH}`);
  const headSha = ref.object.sha;
  const commit  = await get(`${API}/repos/${REPO}/git/commits/${headSha}`);
  const treeSha = commit.tree.sha;
  console.log(`HEAD: ${headSha}  tree: ${treeSha}`);

  // 2. Create a blob for each file
  const treeItems = [];
  for (const filePath of files) {
    const content = fs.readFileSync(filePath);
    process.stdout.write(`  blob ${filePath} (${content.length} bytes)... `);
    const blob = await post(`${API}/repos/${REPO}/git/blobs`, {
      content:  content.toString('base64'),
      encoding: 'base64',
    });
    console.log(blob.sha);
    const execBit = fs.statSync(filePath).mode & 0o111;
    treeItems.push({ path: filePath, mode: execBit ? '100755' : '100644', type: 'blob', sha: blob.sha });
  }

  // 3. Create new tree
  const newTree = await post(`${API}/repos/${REPO}/git/trees`, {
    base_tree: treeSha,
    tree:      treeItems,
  });
  console.log(`New tree: ${newTree.sha}`);

  // 4. Create commit
  const date      = new Date().toISOString().slice(0, 10);
  const message   = `Daily AIS archive ${date}`;
  const newCommit = await post(`${API}/repos/${REPO}/git/commits`, {
    message,
    tree:    newTree.sha,
    parents: [headSha],
    author:  { name: 'Popeye (NanoClaw)', email: 'popeye@nanoclaw.ai', date: new Date().toISOString() },
  });
  console.log(`New commit: ${newCommit.sha}  "${message}"`);

  // 5. Advance the branch ref
  await patch(`${API}/repos/${REPO}/git/refs/heads/${BRANCH}`, { sha: newCommit.sha });
  console.log(`✓ Pushed — https://github.com/${REPO}/commit/${newCommit.sha}`);

  // 6. Record locally so git status doesn't re-surface these files next run
  execSync(`git add ${files.map(f => `"${f}"`).join(' ')}`);
  try {
    execSync(`git commit -m "${message} [local sync]" --allow-empty`, { stdio: 'pipe' });
  } catch { /* nothing to commit, that's fine */ }
}

main().catch(e => { console.error('FATAL:', e.message); process.exit(1); });
