const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(confirmed, failure = false) {
  const elements = {};
  const calls = [], notices = [];
  let click, refreshes = 0;
  const scope = {
    document: {
      querySelector: id => elements[id] ||= { value: '', innerHTML: '' },
      addEventListener: (_, handler) => { click = handler; },
    },
    window: { confirm: message => { assert.match(message, /物理删除.*全部手牌/); return confirmed; } },
    api: async (...args) => { calls.push(args); if (failure) throw new Error('删除失败'); },
    esc: String,
  };
  const source = fs.readFileSync(path.join(__dirname, '../../app/static/js/tenants.js'), 'utf8');
  vm.runInNewContext(source.replace(/^import .*;\n/gm, '').replace('export ', '') + '\nglobalThis.setup = setupTenants;', scope);
  let tenants = [{tenant_id: '100001', note: '', round_version: 1, enabled: true}];
  const ui = scope.setup({
    getTenants: () => tenants,
    refresh: async () => { refreshes++; tenants = []; },
    toast: text => notices.push(text), showModal() {}, closeModal() {},
  });
  ui.render(tenants);
  assert.match(elements['#tenantRows'].innerHTML, /data-delete-tenant="100001"/);
  const button = {dataset: {deleteTenant: '100001'}, hasAttribute: () => false};
  return { calls, notices, elements, refreshes: () => refreshes,
    click: () => click({target: {closest: () => button}}) };
}

test('cancel deletion leaves tenant untouched', async () => {
  const ui = setup(false);
  await ui.click();
  assert.equal(ui.calls.length, 0);
  assert.equal(ui.refreshes(), 0);
});
test('confirmed deletion calls DELETE and refreshes tenant list', async () => {
  const ui = setup(true);
  await ui.click();
  assert.equal(ui.calls[0][0], '/api/tenants/100001');
  assert.equal(ui.calls[0][1].method, 'DELETE');
  assert.equal(ui.refreshes(), 1);
  assert.doesNotMatch(ui.elements['#tenantRows'].innerHTML, /data-delete-tenant/);
});
test('failed deletion keeps tenant visible and allows retry', async () => {
  const ui = setup(true, true);
  await ui.click();
  assert.equal(ui.notices[0], '删除失败');
  assert.equal(ui.refreshes(), 0);
  assert.match(ui.elements['#tenantRows'].innerHTML, /data-delete-tenant="100001"/);
  await ui.click();
  assert.equal(ui.calls.length, 2);
});
