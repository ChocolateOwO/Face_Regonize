// Node-only behavior checks: no browser server, credentials or real downloads.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/components/PhotoBatchDownload.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;

function setup({ status = 'syncing_drive', ready = true, stopping = false, failWrite = false, cancel = false, unauthorized = false } = {}) {
  const calls = [];
  const errors = [];
  const writer = {
    write: async chunk => { calls.push(['write', [...chunk]]); if (failWrite) throw Error('disk full'); },
    close: async () => calls.push(['close']),
    abort: async () => calls.push(['abort']),
  };
  globalThis.window = {
    showSaveFilePicker: async () => {
      calls.push(['picker']);
      if (cancel) throw new DOMException('cancel', 'AbortError');
      return { createWritable: async () => writer };
    },
    dispatchEvent: event => calls.push([event.type]),
  };
  globalThis.localStorage = { getItem: () => 'isolated-token' };
  globalThis.fetch = async (url, options) => {
    calls.push(['fetch', url]);
    assert.equal(options.headers.Authorization, 'Bearer isolated-token');
    return {
      ok: !unauthorized, status: unauthorized ? 401 : 200, statusText: 'Unauthorized',
      json: async () => ({ detail: 'Not authenticated' }),
      body: new ReadableStream({ start(controller) {
        controller.enqueue(new Uint8Array([1, 2])); controller.enqueue(new Uint8Array([3])); controller.close();
      } }),
      blob: () => { throw Error('Large ZIP must not be buffered'); },
    };
  };
  class ApiError extends Error {}
  const jsx = (type, props) => ({ type, props });
  const exports = {};
  new Function('require', 'exports', compiled)(name => {
    if (name === 'react') return { useState: initial => [initial, value => { if (typeof value === 'string') errors.push(value); }] };
    if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (name === '../api/client') return { API_BASE: '', ApiError };
    if (name === './ui') return { Button: 'Button' };
    throw Error(name);
  }, exports);
  const element = exports.default({ id: 'a'.repeat(32), ready, status, stopping });
  const button = element.props.children[0];
  return { calls, errors, button };
}

test('syncing/completed/Drive-failed remain enabled; incomplete/stopping disabled', () => {
  for (const status of ['syncing_drive', 'completed', 'failed']) assert.equal(setup({ status }).button.props.disabled, false);
  for (const status of ['pending', 'processing', 'stopping', 'deleting', 'cancelled']) assert.equal(setup({ status }).button.props.disabled, true);
  assert.equal(setup({ ready: false }).button.props.disabled, true);
  assert.equal(setup({ stopping: true }).button.props.disabled, true);
});
test('large ZIP writes chunks to disk, never Blob; picker precedes fetch', async () => {
  const { button, calls } = setup();
  await button.props.onClick();
  assert.deepEqual(calls.map(c => c[0]), ['picker', 'fetch', 'write', 'write', 'close']);
  assert.deepEqual(calls.filter(c => c[0] === 'write').map(c => c[1]), [[1, 2], [3]]);
});
test('cancelled picker starts no download', async () => {
  const { button, calls, errors } = setup({ cancel: true });
  await button.props.onClick();
  assert.deepEqual(calls, [['picker']]);
  assert.deepEqual(errors, ['']);
});
test('write failure aborts partial file', async () => {
  const { button, calls } = setup({ failWrite: true });
  await button.props.onClick();
  assert.equal(calls.at(-1)[0], 'abort');
  assert.equal(calls.some(c => c[0] === 'close'), false);
});
test('expired authentication keeps existing unauthorized event', async () => {
  const { button, calls } = setup({ unauthorized: true });
  await button.props.onClick();
  assert.equal(calls.at(-1)[0], 'auth:unauthorized');
  assert.equal(calls.some(c => c[0] === 'write'), false);
});
