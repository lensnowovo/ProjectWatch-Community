import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const view = readFileSync(new URL('../src/landscape.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');

test('registry overlaps are labelled as record comparisons, not confirmed competition', () => {
  assert.match(view, /临床登记对照/);
  assert.match(view, /不等于已确认的竞争关系/);
  assert.match(view, /same_target_and_modality: "同靶点、同技术形式"/);
  assert.doesNotMatch(view, /registry[^\n]*supported/);
});

test('registry rows honor company-wide removal and show the registry id link', () => {
  assert.match(view, /export function registryRows/);
  assert.match(view, /removed\.has\(peer\.name\.toLocaleLowerCase\(\)\)/);
  assert.match(view, /product\.nct_ids\.map/);
  assert.match(view, /<RegistryComparison detail=\{detail\} \/>/);
});

test('API types keep candidate origin and registry exclusions', () => {
  assert.match(api, /origin\?: "search" \| "registry"/);
  assert.match(api, /excluded: \{ non_industry: number; self: number; user_removed: number; own_product\?: number; grouped_alternatives\?: number \}/);
});

test('the basis of our own comparison descriptors is shown', () => {
  assert.match(view, /本公司对照依据/);
  assert.match(view, /参考本公司登记记录/);
});

test('indication comparison and per-trial records are shown without union of arms', () => {
  assert.match(view, /同一疾病类别/);
  assert.match(view, /试验组未注明适应症/);
  assert.match(view, /不等于已核实的产品所有人/);
  assert.match(view, /product\.trials/);
  assert.doesNotMatch(view, /indications\.slice\(0, 3\)/);
});

test('search-discovered names are shown only as unverified leads', () => {
  assert.match(view, /其他线索（来自搜索摘要，未核实）/);
  assert.match(api, /web_peer_research\?: string/);
});

test('rival cards mark search-discovered names as unverified leads', () => {
  assert.match(view, />搜索线索</);
  assert.match(view, /尚未核实是否为竞对/);
});

test('registry peers summarise their card and leads sort last', () => {
  assert.match(view, /registryByPeer/);
  assert.match(view, /orderedPeers\.map/);
  assert.match(view, /名字出自搜索摘要，尚无核实资料/);
});

test('Google News is an opt-in news source in settings', () => {
  const settings = readFileSync(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8');
  assert.match(settings, /同时使用 Google News/);
  assert.match(settings, /google_news: false/);
});
