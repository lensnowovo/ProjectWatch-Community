# Studio 设计对照版

使用 8933 的页面结构与设计语言，复用 8945 的模拟数据、来源阅读和分析图表。

从 frontend 目录运行：

```powershell
node node_modules/vite/bin/vite.js --config studio-concept/vite.config.ts
node node_modules/typescript/bin/tsc --noEmit -p studio-concept/tsconfig.json
node node_modules/vite/bin/vite.js build --config studio-concept/vite.config.ts
node --test studio-concept/*.test.mjs
```

打开 http://127.0.0.1:8946/#/project/chengyao/overview 。只使用虚构数据，不连接后端。上传只读取 File.name；不读取文件内容、不发送请求。构建产物位于本目录 dist，不影响主产品或 prototype/dist。
