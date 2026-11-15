const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { test } = require('node:test');

const workflow = fs.readFileSync(path.join(__dirname, '../workflows/blocked-contributors.yml'), 'utf8');
const script = workflow.slice(workflow.indexOf('            const event = context.payload;'));
const functions = [
  ['async function forkCleanupToken(', 'async function cleanSubmissionBranch('],
  ['async function repairCatalogIndex(', 'async function finishPublication('],
].map(([start, end]) => script.slice(script.indexOf(start), script.indexOf(end))).join('\n');

const original = 'a'.repeat(40);
const main = 'b'.repeat(40);
const repaired = 'c'.repeat(40);
const concurrent = 'd'.repeat(40);
const branch = 'nzik/theme-9fabc1ee-37b1-4d82-8d3b-f7b141ceb831';
const fork = { id: 2, owner: { id: 101, login: 'Creator' }, name: 'N-Zik-Theme-Store', default_branch: 'main' };

function harness({ race, missingInstallation = false, environment = {} } = {}) {
  let head = original;
  let reads = 0;
  const commands = [];
  const apiCalls = [];
  const appCalls = [];
  const secrets = [];
  const failures = [];
  const statuses = [];
  const removed = [];
  const temporaryRoot = path.resolve(os.tmpdir());
  const temporaryDirectory = path.join(temporaryRoot, 'nzik-fork-ref-test');
  const current = { head: { repo: fork, ref: branch, sha: original }, base: { sha: main } };
  const context = {
    Buffer,
    process: { env: { PATH: process.env.PATH, THEME_STORE_GITHUB_APP_PRIVATE_KEY: 'private-key', ...environment } },
    event: { repository: { id: 1 } },
    context: { repo: { owner: 'N-Zik-Group', repo: 'N-Zik-Theme-Store' } },
    pull: { number: 7, user: { id: 101 } },
    indexPath: 'catalog/themes/index.json',
    currentLabels: new Set(['accepted']),
    target: { issue_number: 7 },
    core: {
      setSecret: value => secrets.push(value),
      setFailed: value => failures.push(value),
      notice: () => {},
      warning: () => {},
    },
    status: async value => statuses.push(value),
    fetch: async (url, options) => {
      appCalls.push({ url, options });
      if (url.endsWith('/installation')) return {
        status: missingInstallation ? 404 : 200, ok: !missingInstallation,
        json: async () => ({ id: 501, account: { id: fork.owner.id }, permissions: { contents: 'write' } }),
      };
      return { status: 201, ok: true, json: async () => ({ token: 'installation-token' }) };
    },
    require: name => {
      if (name === 'node:crypto') return { sign: () => Buffer.from('signature') };
      if (name === 'node:fs') return {
        mkdtempSync: prefix => { assert.equal(prefix, path.join(temporaryRoot, 'nzik-fork-ref-')); return temporaryDirectory; },
        rmSync: (directory, options) => removed.push({ directory, options }),
      };
      if (name === 'node:child_process') return {
        execFileSync: (command, args, options) => {
          commands.push({ command, args, options });
          if (args[0] === 'fetch' && race === 'before-push') head = concurrent;
          if (args[0] !== 'push') return Buffer.alloc(0);
          assert.equal(args[1], `--force-with-lease=refs/heads/${branch}:${original}`);
          if (head !== original) throw new Error('stale info');
          head = args.at(-1).split(':')[0] || null;
          return Buffer.alloc(0);
        },
      };
      return require(name);
    },
    github: { rest: {
      git: {
        getRef: async args => {
          apiCalls.push({ method: 'getRef', args });
          const result = { data: { object: { sha: head } } };
          if (++reads === 1 && race === 'after-read') head = concurrent;
          return result;
        },
        getCommit: async args => { apiCalls.push({ method: 'getCommit', args }); return { data: { tree: { sha: main } } }; },
        createTree: async args => { apiCalls.push({ method: 'createTree', args }); return { data: { sha: repaired } }; },
        createCommit: async args => { apiCalls.push({ method: 'createCommit', args }); return { data: { sha: repaired } }; },
        updateRef: async () => { assert.fail('Ref writes must use a Git lease'); },
      },
      pulls: { get: async () => ({ data: { head: { sha: head }, changed_files: 0 } }) },
      issues: { removeLabel: async args => apiCalls.push({ method: 'removeLabel', args }) },
    } },
  };
  vm.createContext(context);
  vm.runInContext(functions, context);
  return { context, current, commands, apiCalls, appCalls, secrets, failures, statuses, removed, head: () => head };
}

test('moderation workflow script parses', () => {
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  assert.doesNotThrow(() => new AsyncFunction('github', 'context', 'core', script));
});

test('index repair uses a repository-scoped App token and leases the checked head', async () => {
  const h = harness();
  await h.context.repairCatalogIndex(h.current, { schemaVersion: 1, themes: [] }, []);
  assert.equal(h.head(), repaired);
  assert.equal(h.appCalls.length, 2);
  assert.deepEqual(JSON.parse(h.appCalls[1].options.body), { repository_ids: [fork.id], permissions: { contents: 'write' } });
  for (const call of h.apiCalls.filter(call => ['getRef', 'createTree', 'createCommit'].includes(call.method))) {
    assert.equal(call.args.headers.authorization, 'Bearer installation-token');
  }
  const commit = h.apiCalls.find(call => call.method === 'createCommit');
  assert.deepEqual(Array.from(commit.args.parents), [original, main]);
  assert.deepEqual(h.statuses, ['in review']);
  assert.ok(h.apiCalls.some(call => call.method === 'removeLabel' && call.args.name === 'accepted'));
  assert.equal(h.failures.length, 0);
});

test('refusal reset uses the App token and confirms an empty pull request diff', async () => {
  const h = harness();
  assert.equal(await h.context.resetSubmissionBranch(h.current), true);
  assert.equal(h.head(), main);
  assert.equal(h.appCalls.length, 2);
  assert.ok(h.apiCalls.filter(call => call.method === 'getRef')
    .every(call => call.args.headers.authorization === 'Bearer installation-token'));
});

for (const race of ['after-read', 'before-push']) {
  for (const operation of ['reset', 'repair']) {
    test(`${operation} preserves new work pushed ${race}`, async () => {
      const h = harness({ race });
      const promise = operation === 'reset'
        ? h.context.resetSubmissionBranch(h.current)
        : h.context.repairCatalogIndex(h.current, { schemaVersion: 1, themes: [] }, []);
      await assert.rejects(promise, /whether its head moved/);
      assert.equal(h.head(), concurrent);
      assert.equal(h.statuses.length, 0);
      assert.equal(h.removed.length, 1);
    });
  }
}

test('branch deletion uses the same explicit lease without fetching a replacement', () => {
  const h = harness();
  h.context.writeSubmissionRef(fork, branch, original, null, 'installation-token');
  assert.equal(h.head(), null);
  assert.deepEqual(h.commands.map(call => call.args[0]), ['init', 'push']);
});

test('Git credentials are masked and isolated from subprocess arguments and other secrets', async () => {
  const h = harness({ environment: { THEME_STORE_CLEANUP_TOKEN: 'legacy-token', THEME_STORE_MERGE_TOKEN: 'upstream-token' } });
  await h.context.resetSubmissionBranch(h.current);
  const encoded = Buffer.from('x-access-token:installation-token').toString('base64');
  assert.ok(h.secrets.includes('installation-token'));
  assert.ok(h.secrets.includes(encoded));
  for (const call of h.commands) {
    assert.equal(call.command, 'git');
    assert.equal(call.options.env.GIT_CONFIG_VALUE_0, 'AUTHORIZATION: basic ' + encoded);
    assert.equal(call.options.env.GIT_CONFIG_NOSYSTEM, '1');
    assert.equal(call.options.env.GIT_CONFIG_GLOBAL, path.join(call.options.cwd, 'global.config'));
    const args = JSON.stringify(call.args);
    for (const secret of ['installation-token', encoded, 'legacy-token', 'private-key', 'upstream-token']) {
      assert.ok(!args.includes(secret));
    }
    for (const name of ['THEME_STORE_CLEANUP_TOKEN', 'THEME_STORE_GITHUB_APP_PRIVATE_KEY', 'THEME_STORE_MERGE_TOKEN']) {
      assert.equal(call.options.env[name], undefined);
    }
  }
  assert.equal(h.removed.length, 1);
});

test('missing installation stops repair and reset before any branch write', async () => {
  const h = harness({ missingInstallation: true });
  await h.context.repairCatalogIndex(h.current, { themes: [] }, []);
  assert.match(h.failures[0], /N-Zik Sign In installed/);
  await assert.rejects(h.context.resetSubmissionBranch(h.current), /N-Zik Sign In installed/);
  assert.equal(h.commands.length, 0);
  assert.equal(h.head(), original);
});

test('legacy cleanup token and upstream workflow token retain their existing roles', async () => {
  const h = harness({ environment: {
    THEME_STORE_GITHUB_APP_PRIVATE_KEY: '', THEME_STORE_CLEANUP_TOKEN: 'legacy-token', THEME_STORE_MERGE_TOKEN: 'upstream-token',
  } });
  assert.equal(await h.context.forkCleanupToken(fork), 'legacy-token');
  assert.equal(await h.context.forkCleanupToken({ ...fork, id: 1 }), 'upstream-token');
  assert.equal(h.appCalls.length, 0);
});

test('ref writes reject default branches and malformed inputs before starting Git', () => {
  const h = harness();
  for (const args of [
    [fork, 'main', original, main, 'token'],
    [{ ...fork, default_branch: branch }, branch, original, main, 'token'],
    [fork, branch, 'bad-sha', main, 'token'],
    [fork, branch, original, '--upload-pack=evil', 'token'],
    [fork, 'nzik/../main', original, main, 'token'],
    [{ ...fork, owner: { login: '-upload/pack' } }, branch, original, main, 'token'],
  ]) assert.throws(() => h.context.writeSubmissionRef(...args), /Invalid submission branch update/);
  assert.equal(h.commands.length, 0);
});

test('real Git leases protect reset, repair and deletion on a local bare remote', t => {
  const temporaryRoot = path.resolve(os.tmpdir());
  const root = fs.mkdtempSync(path.join(temporaryRoot, 'nzik-moderation-test-'));
  t.after(() => {
    assert.equal(path.dirname(root), temporaryRoot);
    fs.rmSync(root, { recursive: true, force: true });
  });
  const remote = path.join(root, 'remote.git');
  execFileSync('git', ['init', '--quiet', '--bare', remote]);
  const git = (...args) => execFileSync('git', ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', ...args], {
    cwd: remote, input: '', stdio: ['pipe', 'pipe', 'pipe'],
  }).toString().trim();
  const tree = git('mktree');
  const initial = git('commit-tree', tree, '-m', 'Original submission');
  const base = git('commit-tree', tree, '-m', 'Base');
  const replacement = git('commit-tree', tree, '-p', initial, '-p', base, '-m', 'Repaired index');
  const newWork = git('commit-tree', tree, '-p', initial, '-m', 'New work');
  git('update-ref', 'refs/heads/main', base);
  const ref = `refs/heads/${branch}`;
  const publicUrl = `https://github.com/${fork.owner.login}/${fork.name}.git`;
  const localUrl = pathToFileURL(remote).href;
  let changeBeforePush = false;
  const context = {
    Buffer, process: { env: { PATH: process.env.PATH } }, core: { setSecret: () => {} },
    require: name => name !== 'node:child_process' ? require(name) : {
      execFileSync: (command, args, options) => {
        const localArgs = args.map(value => value === publicUrl ? localUrl : value);
        assert.ok(localArgs.every(value => !value.startsWith('https://')));
        if (args[0] === 'push' && changeBeforePush) git('update-ref', ref, newWork);
        try {
          return execFileSync(command, localArgs, options);
        } catch (error) {
          if (!changeBeforePush) t.diagnostic(`${args[0]}: ${error.stderr?.toString() ?? error.message}`);
          throw error;
        }
      },
    },
  };
  vm.createContext(context);
  vm.runInContext(script.slice(script.indexOf('function writeSubmissionRef('), script.indexOf('async function cleanSubmissionBranch(')), context);
  for (const next of [base, replacement, null]) {
    git('update-ref', ref, initial);
    changeBeforePush = false;
    context.writeSubmissionRef(fork, branch, initial, next, 'test-token');
    if (next === null) assert.throws(() => git('rev-parse', '--verify', ref));
    else assert.equal(git('rev-parse', ref), next);
    git('update-ref', ref, initial);
    changeBeforePush = true;
    assert.throws(() => context.writeSubmissionRef(fork, branch, initial, next, 'test-token'), /whether its head moved/);
    assert.equal(git('rev-parse', ref), newWork);
  }
});
