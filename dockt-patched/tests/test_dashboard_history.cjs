const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../server/static/index.html'), 'utf8');
const shared = html.slice(html.indexOf('const CHATS_KEY'), html.indexOf('function renderChatList()'));
const route = html.slice(html.indexOf('async function route()'), html.indexOf('window.addEventListener("hashchange", route)'));

function harness() {
  const rows = new Map(), storage = new Map(), calls = [];
  let fail = false, renders = 0;
  const context = vm.createContext({
    messages: [], busy: false, crypto: {randomUUID: () => 'dashboard-id'},
    console: {warn() {}}, URLSearchParams,
    location: {hash: '', pathname: '/'}, history: {replaceState() {}},
    localStorage: {getItem: k => storage.get(k), removeItem: k => storage.delete(k)},
    renderConversation: () => renders++, renderChatList() {}, closeSidebar() {},
    $: () => ({focus() {}}), sendChat: () => {throw Error('must not re-ask');},
    api: async (url, options = {}) => {
      calls.push([url, options.method || 'GET']);
      if (fail) throw Error('offline');
      if (url === '/api/conversations') return structuredClone([...rows.values()]);
      const id = decodeURIComponent(url.split('/').at(-1));
      if (options.method === 'PUT') {
        const messages = JSON.parse(options.body).messages;
        rows.set(id, {id, messages, ts: (rows.get(id)?.ts || 0)+1});
      }
      return structuredClone(rows.get(id));
    },
  });
  vm.runInContext(shared + route, context);
  return {context, rows, storage, calls, setFail: v => fail = v,
    renders: () => renders, run: code => vm.runInContext(code, context)};
}

test('all dashboard inline JavaScript parses', () => {
  for (const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(m[1]);
});
test('dashboard discovers pet history and refreshes an open conversation', async () => {
  const h = harness();
  h.rows.set('pet-id', {id: 'pet-id', ts: 2, messages: [{role:'user', content:'From pet'}]});
  h.run('chatId = "pet-id"');
  await h.run('refreshChats()');
  assert.equal(h.run('messages[0].content'), 'From pet');
  assert.equal(h.run('loadChats().length'), 1);
  assert.equal(h.renders(), 1);
});
test('polling never overwrites an in-flight answer', async () => {
  const h = harness();
  h.rows.set('pet-id', {id:'pet-id', ts:3, messages:[]});
  h.run('chatId = "pet-id"; busy = true; messages = [{content:"streaming"}]');
  await h.run('refreshChats()');
  assert.equal(h.run('messages[0].content'), 'streaming');
  assert.equal(h.renders(), 0);
});
test('dashboard saves full messages including draft metadata to shared storage', async () => {
  const h = harness();
  h.run('messages = [{role:"assistant", content:"draft", draft:true}]');
  await h.run('saveChat()');
  assert.equal(h.rows.get('dashboard-id').messages[0].draft, true);
});
test('legacy migration retains history on failure and safely retries', async () => {
  const h = harness();
  h.storage.set('dockt-chats', JSON.stringify([{id:4, messages:[{role:'user', content:'old'}]}]));
  h.setFail(true);
  await h.run('migrateLocalChats()');
  assert.ok(h.storage.has('dockt-chats'));
  h.setFail(false);
  await h.run('migrateLocalChats()');
  assert.equal(h.storage.has('dockt-chats'), false);
  assert.equal(h.rows.get('legacy-4').messages[0].content, 'old');
});
test('pet deep link opens saved conversation without generating a second answer', async () => {
  const h = harness();
  h.rows.set('pet-id', {id:'pet-id', ts:3, messages:[{role:'assistant', content:'saved answer'}]});
  h.context.location.hash = '#/conversation/pet-id';
  await h.run('route()');
  assert.equal(h.run('chatId'), 'pet-id');
  assert.equal(h.run('messages[0].content'), 'saved answer');
  assert.ok(h.calls.every(([url]) => url.startsWith('/api/conversations')));
});
