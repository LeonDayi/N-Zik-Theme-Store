const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { creatorSubmission, themeSubmission, trustedCandidate } = require('./validate_submission.cjs');

const id = 101;
const login = 'Creator';
const themeId = '9fabc1ee-37b1-4d82-8d3b-f7b141ceb831';
const themePath = `catalog/themes/${themeId}/`;
const themeIndex = 'catalog/themes/index.json';
const profilePath = `catalog/creators/${id}/profile.json`;
const creatorIndex = 'catalog/creators/index.json';
const encode = value => Buffer.from(JSON.stringify(value));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const file = (filename, status = 'added') => ({ filename, status });

function makeCandidate() {
  const revisions = Object.fromEntries(['ancestor', 'base', 'head'].map(ref => [ref, new Map()]));
  const put = (ref, filename, value) => {
    const bytes = encode(value);
    revisions[ref].set(filename, bytes);
    return hash(bytes);
  };
  const candidate = {
    ancestor: 'ancestor', base: 'base', head: 'head', changes: [],
    read(ref, filename) {
      assert.ok(revisions[ref].has(filename), `Missing fixture ${ref}:${filename}`);
      return revisions[ref].get(filename);
    },
  };
  return { candidate, put };
}

function themeFixture({ published = false, submittedOwner = id, publishedOwner = id, publishedLogin = login } = {}) {
  const { candidate, put } = makeCandidate();
  const revision = published ? 2 : 1;
  const manifestFile = `${themePath}${revision}/manifest.json`;
  const oldManifest = { themeId, revision: 1, author: { githubId: publishedOwner, login: publishedLogin } };
  const previous = published ? [{
    themeId, revision: 1, manifestSha256: put('base', `${themePath}1/manifest.json`, oldManifest),
  }] : [];
  const manifest = { themeId, revision, author: { githubId: submittedOwner, login },
    appearance: { path: 'appearance.json' }, previews: [{ path: 'previews/player-normal.webp' }] };
  const entry = { themeId, revision, manifestSha256: put('head', manifestFile, manifest) };
  const reviewsFile = `${themePath}${revision}/reviews.json`;
  put('head', reviewsFile, { schemaVersion: 1, reviews: [] });
  put('ancestor', themeIndex, { schemaVersion: 1, themes: previous });
  put('base', themeIndex, { schemaVersion: 1, themes: previous });
  put('head', themeIndex, { schemaVersion: 1, themes: [entry] });
  candidate.changes = [
    file(themeIndex, 'modified'), file(manifestFile), file(`${themePath}${revision}/appearance.json`),
    file(`${themePath}${revision}/previews/player-normal.webp`), file(reviewsFile),
  ];
  return { candidate, put, entry };
}

test('a creator can publish and update their own theme', () => {
  for (const published of [false, true]) {
    const { candidate, entry } = themeFixture({ published });
    assert.deepEqual(themeSubmission(candidate, id, login, themeId).index.themes, [entry]);
  }
});

test('an updated theme uses the authenticated current login after a GitHub rename', () => {
  const { candidate, entry } = themeFixture({ published: true, publishedLogin: 'Previous-Creator' });
  assert.deepEqual(themeSubmission(candidate, id, login, themeId).index.themes, [entry]);
  assert.throws(() => themeSubmission(candidate, id, 'Previous-Creator', themeId), /author/);
});

test('theme approval rejects unrelated files and changes to existing revisions', () => {
  for (const unexpected of [file('docs/note.md'), file(`${themePath}1/appearance.json`, 'modified')]) {
    const { candidate } = themeFixture({ published: true });
    candidate.changes.push(unexpected);
    assert.throws(() => themeSubmission(candidate, id, login, themeId), /only change the index/);
  }
});

test('theme approval rejects reviews included in a new revision', () => {
  const { candidate, put } = themeFixture();
  put('head', `${themePath}1/reviews.json`, { schemaVersion: 1, reviews: [{ author: { githubId: id } }] });
  assert.throws(() => themeSubmission(candidate, id, login, themeId), /empty review list/);
});

test('theme approval validates the new and previous owners', () => {
  for (const options of [{ submittedOwner: 102 }, { published: true, publishedOwner: 102 }]) {
    const { candidate } = themeFixture(options);
    assert.throws(() => themeSubmission(candidate, id, login, themeId), /author|owner/);
  }
});

test('theme index repair preserves unrelated entries added to main', () => {
  const { candidate, put, entry } = themeFixture();
  const unrelated = { themeId: '087ff26b-5ebb-4a41-97ca-b45134306be3', revision: 1 };
  put('base', themeIndex, { schemaVersion: 1, themes: [unrelated] });
  assert.deepEqual(themeSubmission(candidate, id, login, themeId).index.themes, [unrelated, entry]);
  put('head', themeIndex, { schemaVersion: 1, themes: [unrelated, entry] });
  assert.throws(() => themeSubmission(candidate, id, login, themeId), /other index entries/);
});

test('creator profile may replace its own referenced artwork and remove the old artwork', () => {
  const { candidate, put } = makeCandidate();
  const oldProfile = { githubId: id, login, avatar: { source: 'custom', asset: { path: 'old.webp' } } };
  const profile = { githubId: id, login, avatar: { source: 'custom', asset: { path: 'new.webp' } } };
  const previous = { schemaVersion: 1, creators: [{ githubId: id, profileSha256: put('base', profilePath, oldProfile) }] };
  put('ancestor', creatorIndex, previous);
  put('base', creatorIndex, previous);
  const updated = { githubId: id, profileSha256: put('head', profilePath, profile) };
  put('head', creatorIndex, { ...previous, creators: [updated] });
  const directory = `catalog/creators/${id}/`;
  candidate.changes = [file(creatorIndex, 'modified'), file(profilePath, 'modified'),
    file(directory + 'old.webp', 'removed'), file(directory + 'new.webp')];
  assert.doesNotThrow(() => creatorSubmission(candidate, id, login));
  candidate.changes.push(file(directory + 'unreferenced.webp'));
  assert.throws(() => creatorSubmission(candidate, id, login), /referenced images/);
  candidate.changes.pop();
  put('head', creatorIndex, { ...previous, creators: [updated, { githubId: 102 }] });
  assert.throws(() => creatorSubmission(candidate, id, login), /other index entries/);
});

test('trusted checkout validates merged data for the exact approved PR head', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nzik-submission-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const run = (...args) => execFileSync('git', args, { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
  run('init', '-q');
  run('config', 'user.name', 'Test');
  run('config', 'user.email', 'test@example.com');
  run('config', 'core.autocrlf', 'false');
  run('config', 'core.filemode', 'false');
  run('remote', 'add', 'origin', '.');
  fs.mkdirSync(path.join(root, '.github', 'scripts'), { recursive: true });
  fs.mkdirSync(path.join(root, 'catalog'));
  fs.writeFileSync(path.join(root, '.github', 'scripts', 'validate_catalog.py'),
    'from pathlib import Path\nassert Path("catalog/submission.json").read_bytes() == b\'{"expected":true}\'\n');
  run('add', '.');
  run('commit', '-qm', 'Trusted validator');
  const base = run('rev-parse', 'HEAD');
  fs.writeFileSync(path.join(root, 'catalog', 'submission.json'), '{"expected":true}');
  run('add', '.');
  run('commit', '-qm', 'Submit catalog data');
  const head = run('rev-parse', 'HEAD');
  run('update-ref', 'refs/pull/4/head', head);
  run('checkout', '--detach', '-q', base);
  const pr = { number: 4, base: { sha: base }, head: { sha: head } };
  assert.throws(() => trustedCandidate(root, { ...pr, head: { sha: base } }), /changed after approval/);
  const candidate = trustedCandidate(root, pr);
  assert.deepEqual(candidate.changes, [file('catalog/submission.json')]);
  assert.doesNotThrow(() => candidate.validateMerged());
  assert.equal(run('rev-parse', 'HEAD'), base);

  // Simulate a prior approval advancing main while this approval was queued.
  run('reset', '--hard', base);
  fs.writeFileSync(path.join(root, 'catalog', 'earlier-publication.json'), '{}');
  const validator = path.join(root, '.github', 'scripts', 'validate_catalog.py');
  fs.appendFileSync(validator, 'assert Path("catalog/earlier-publication.json").is_file()\n');
  run('add', '.');
  run('commit', '-qm', 'Earlier publication');
  const latestBase = run('rev-parse', 'HEAD');
  assert.throws(() => trustedCandidate(root, pr), /Main changed/);
  const queued = trustedCandidate(root, { ...pr, base: { sha: latestBase } });
  assert.deepEqual(queued.changes, [file('catalog/submission.json')]);
  assert.doesNotThrow(() => queued.validateMerged());
  assert.equal(run('rev-parse', 'HEAD'), latestBase);
  run('update-ref', 'refs/heads/main', latestBase);
  assert.throws(() => trustedCandidate(root, { ...pr, base: { sha: latestBase } })
    .publishValidatedMerge('Unvalidated merge'), /must pass/);

  const tree = run('rev-parse', 'HEAD^{tree}');
  const racingCommit = run('commit-tree', tree, '-p', latestBase, '-m', 'Independent publication');
  run('update-ref', 'refs/heads/main', racingCommit);
  assert.throws(() => queued.publishValidatedMerge('Stale approval'), /stale info/);
  assert.equal(run('rev-parse', 'refs/heads/main'), racingCommit);

  run('reset', '--hard', latestBase);
  run('checkout', '--detach', '-q', racingCommit);
  const retry = trustedCandidate(root, { ...pr, base: { sha: racingCommit } });
  retry.validateMerged();
  const merged = retry.publishValidatedMerge('Revalidated approval');
  assert.equal(run('rev-parse', 'refs/heads/main'), merged);
  assert.equal(run('rev-parse', merged + '^1'), racingCommit);
  assert.equal(run('rev-parse', merged + '^2'), head);
});

test('a repaired index validates and publishes current main without writing its workflows into the fork', t => {
  const temporaryRoot = path.resolve(os.tmpdir());
  const root = fs.mkdtempSync(path.join(temporaryRoot, 'nzik-repaired-submission-'));
  t.after(() => {
    assert.equal(path.dirname(root), temporaryRoot);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const git = (...args) => execFileSync('git', args, { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
  const put = (filename, value) => {
    fs.mkdirSync(path.dirname(path.join(root, filename)), { recursive: true });
    fs.writeFileSync(path.join(root, filename), value);
  };
  git('init', '-q');
  git('config', 'user.name', 'Test');
  git('config', 'user.email', 'test@example.com');
  git('config', 'core.autocrlf', 'false');
  git('remote', 'add', 'origin', '.');
  put('.github/scripts/validate_catalog.py', [
    'import json', 'from pathlib import Path',
    'index = json.loads(Path("catalog/themes/index.json").read_text())',
    'assert index == {"schemaVersion": 1, "themes": ["main", "submission"]}',
    'assert Path("catalog/themes/submission.json").read_text() == "checked data"',
    'assert Path(".github/workflows/test.yml").read_text() == "current trusted workflow"',
  ].join('\n'));
  put('.github/workflows/test.yml', 'old workflow already in the fork');
  put(themeIndex, JSON.stringify({ schemaVersion: 1, themes: [] }));
  git('add', '.');
  git('commit', '-qm', 'Shared ancestor');
  const ancestor = git('rev-parse', 'HEAD');
  put(themeIndex, JSON.stringify({ schemaVersion: 1, themes: ['submission'] }));
  put('catalog/themes/submission.json', 'checked data');
  git('add', '.');
  git('commit', '-qm', 'Accepted fork submission');
  const head = git('rev-parse', 'HEAD');
  git('update-ref', 'refs/pull/4/head', head);
  git('checkout', '--detach', '-q', ancestor);
  put(themeIndex, JSON.stringify({ schemaVersion: 1, themes: ['main'] }));
  put('.github/workflows/test.yml', 'current trusted workflow');
  git('add', '.');
  git('commit', '-qm', 'Concurrent main publication and workflow update');
  const base = git('rev-parse', 'HEAD');
  git('update-ref', 'refs/heads/main', base);
  const candidate = trustedCandidate(root, { number: 4, base: { sha: base }, head: { sha: head } });
  assert.equal(candidate.mergeTree(), null);
  assert.throws(() => candidate.validateRepairedIndex({ schemaVersion: 1, themes: ['submission'] }));
  assert.throws(() => candidate.publishValidatedMerge('Invalid repair'), /must pass/);
  candidate.validateRepairedIndex({ schemaVersion: 1, themes: ['main', 'submission'] });
  const merged = candidate.publishValidatedMerge('Validated index repair');
  assert.equal(git('rev-parse', merged + '^1'), base);
  assert.equal(git('rev-parse', merged + '^2'), head);
  assert.equal(git('show', merged + ':.github/workflows/test.yml'), 'current trusted workflow');
  assert.equal(git('rev-parse', 'refs/pull/4/head'), head);
  assert.equal(git('show', head + ':.github/workflows/test.yml'), 'old workflow already in the fork');
});
