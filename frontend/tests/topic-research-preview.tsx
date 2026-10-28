import React from 'react';
import { createRoot } from 'react-dom/client';
import { TopicResearch } from '../src/TopicResearch';
import type { PlanItem } from '../src/api';
import '../src/styles.css';

// Synthetic fixture; the isolated browser test supplies every API response.
const item = { id: 'synthetic-question', project_id: 'synthetic-project', lifecycle_version: '1',
  text: '合成产品是否获得公开许可', exit_rule: '取得公开原文', status: 'active' } as PlanItem;
createRoot(document.getElementById('root')!).render(<main style={{ maxWidth: 1000, margin: '32px auto', padding: 20 }}>
  <h1>专题研究 · 合成预览</h1><h2>{item.text}</h2><TopicResearch item={item} />
</main>);
