import React from 'react';
import { createRoot } from 'react-dom/client';
import { CollectionSources } from '../src/CollectionSources';
import '../src/styles.css';

// Synthetic fixture. The browser test supplies all API responses.
createRoot(document.getElementById('root')!).render(<main style={{ maxWidth: 1100, margin: '32px auto', padding: 20 }}>
  <h1>来源选择 · 合成预览</h1><CollectionSources projectId="synthetic-source-test" />
</main>);
