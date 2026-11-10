import { expect, test } from '@playwright/test';

for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
  test(`portfolio incomplete valuation ${viewport.width}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    let incomplete = false;
    await page.route('**/api/**', async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (!pathname.startsWith('/api/')) return route.continue();
      let json: unknown = { items: [], total: 0, page: 1, pageSize: 20 };
      if (pathname.endsWith('/auth/status')) {
        json = { authEnabled: false, loggedIn: true, setupState: 'no_password' };
      } else if (pathname.endsWith('/portfolio/accounts')) {
        json = { accounts: [{ id: 1, name: 'Offline demo', market: 'cn', baseCurrency: 'CNY', isActive: true }] };
      } else if (pathname.endsWith('/portfolio/snapshot')) {
        const position = {
          symbol: '600000', market: 'cn', currency: 'CNY', quantity: 10,
          avgCost: 10, totalCost: 100, lastPrice: incomplete ? 0 : 12,
          marketValueBase: incomplete ? 0 : 120, unrealizedPnlBase: incomplete ? 0 : 20,
          unrealizedPnlPct: incomplete ? null : 20, valuationCurrency: 'CNY',
          priceSource: incomplete ? 'missing' : 'history_close', priceDate: '2026-10-09',
          priceAvailable: !incomplete, priceStale: incomplete, valuationComplete: !incomplete,
        };
        json = {
          asOf: '2026-10-09', costMethod: 'fifo', currency: 'CNY', accountCount: 1,
          totalCash: 1000, totalMarketValue: incomplete ? 0 : 120, totalEquity: incomplete ? 1000 : 1120,
          fxStale: incomplete, valuationComplete: !incomplete,
          dataQuality: incomplete ? 'partial' : 'ok', limitations: incomplete ? ['missing_price', 'missing_fx'] : [],
          accounts: [{ accountId: 1, accountName: 'Offline demo', baseCurrency: 'CNY', fxStale: incomplete,
            valuationComplete: !incomplete, positions: [position] }],
        };
      } else if (pathname.endsWith('/portfolio/risk')) {
        json = {
          asOf: '2026-10-09', costMethod: 'fifo', currency: 'CNY', thresholds: {},
          concentration: { available: false, reason: 'incomplete_valuation', topWeightPct: null, topPositions: [] },
          sectorConcentration: { available: false, topWeightPct: null, topSectors: [], coverage: {}, errors: [] },
          drawdown: { available: false, maxDrawdownPct: null, currentDrawdownPct: null, seriesPoints: 0, fxStale: true },
          stopLoss: { available: false, nearAlert: false, triggeredCount: 0, nearCount: 0, items: [] },
          decisionSignalRisk: { available: false, total: 0, actions: {}, items: [] },
        };
      } else if (pathname.endsWith('/portfolio/import/brokers')) {
        json = { brokers: [] };
      }
      await route.fulfill({ json });
    });

    await page.goto('/portfolio');
    await page.waitForLoadState('networkidle');
    await expect(page.getByText('CNY 1,120.00', { exact: true })).toBeVisible();
    const stopLossFlag = page.getByText('止损', { exact: true }).locator('xpath=ancestor::div[contains(@class,"rounded-xl")][1]');
    await expect(stopLossFlag.getByText('正常', { exact: true })).toHaveCount(0);
    await expect(stopLossFlag.getByText('不可用', { exact: true })).toHaveCount(2);
    await page.screenshot({ path: testInfo.outputPath(`complete-${viewport.width}.png`), fullPage: true });

    incomplete = true;
    await page.reload();
    await page.waitForLoadState('networkidle');
    await expect(page.getByText('缺少行情，完整估值不可用；缺少汇率，未进行假定换算', { exact: true })).toBeVisible();
    await expect(page.getByText('CNY 1,000.00', { exact: true })).toHaveCount(0);
    await expect(page.getByText('CNY 0.00', { exact: true })).toHaveCount(0);
    const dashboard = page.locator('section').filter({ has: page.getByText('风险与暴露看板', { exact: true }) });
    await expect(dashboard.getByText('总市值: --')).toBeVisible();
    await expect(dashboard.getByText('正常', { exact: true })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`incomplete-${viewport.width}.png`), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
