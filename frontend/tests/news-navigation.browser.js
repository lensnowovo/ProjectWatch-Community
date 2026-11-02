// Playwright callback for the isolated preview (no application/API traffic).
async (page) => {
  await page.goto('http://127.0.0.1:55182/tests/news-integrity-preview.html');
  const nav = () => page.getByRole('navigation', {name: '外部信息分区', exact: true});
  const errors = [];
  page.on('console', message => {if (message.type() === 'error') errors.push(message.text());});
  await nav().waitFor();
  for (let index = 0; index < 20; index++) {
    const primary = page.getByRole('navigation', {name: '项目内容', exact: true});
    await primary.getByRole('button', {name: '内部信息'}).click();
    await nav().waitFor({state: 'hidden'});
    await primary.getByRole('button', {name: '外部信息'}).click();
    await nav().waitFor();
    if (await nav().count() !== 1) throw new Error('Duplicate category navigation');
  }
  for (let index = 0; index < 25; index++) {
    const before = await page.evaluate(() => Number(document.documentElement.dataset.reads));
    await page.getByRole('button', {name: '刷新状态', exact: true}).click();
    await page.waitForFunction(n => Number(document.documentElement.dataset.reads) > n, before);
    if (await nav().count() !== 1) throw new Error('Duplicate category navigation after poll');
  }
  await page.getByText('近 30 天已收录 4 条公司动态，1 条待评级', {exact: true}).waitFor();
  await page.getByRole('button', {name: '切换日期未知样本'}).click();
  await page.getByText('已收录 1 条公司动态，日期待确认', {exact: true}).waitFor();
  const text = await page.locator('.news-view').innerText();
  if (text.includes('0 条值得关注') || text.includes('近 30 天没有')) throw new Error('Unknown data became a negative conclusion');
  await page.setViewportSize({width: 390, height: 844});
  if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error('Horizontal overflow');
  if (errors.length) throw new Error(errors.join('\n'));
  await page.setViewportSize({width: 1440, height: 1050});
  return {tabCycles: 20, refreshes: 25, navigationCount: await nav().count(), errors};
};
