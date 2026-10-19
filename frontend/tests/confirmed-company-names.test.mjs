import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../src/pages/NewProject.tsx', import.meta.url), 'utf8');
const api = readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8');

test('one confirmation submits only the displayed company abbreviation, not mixed search aliases', () => {
  assert.match(page, /field\("short_name", "公司简称"/);
  assert.match(page, /品牌、产品与其他检索词/);
  assert.match(page, /const companyNames = payload\.short_name \? \[payload\.short_name\] : \[\];/);
  assert.match(page, /api\.createProject\(payload, drafts\.map\(.*points, companyNames\)/);
  assert.doesNotMatch(page, /companyNames\s*=\s*.*aliases/);
});

test('API preserves the distinction between absent alias intent and explicit removal', () => {
  assert.match(api, /confirmedCompanyNames\?: string\[\]/);
  assert.match(api, /confirmedCompanyNames !== undefined \? \{ confirmed_company_names: confirmedCompanyNames \} : \{\}/);
});
