import { expect, test, type Locator } from '@playwright/test';

// Browser-computed text paint matters here: a value assertion alone misses
// -webkit-text-fill-color overriding an otherwise correct CSS color.
async function contrast(input: Locator, placeholder = false) {
  return input.evaluate((element, usePlaceholder) => {
    const style = getComputedStyle(element);
    const textStyle = usePlaceholder ? getComputedStyle(element, '::placeholder') : style;
    const luminance = (color: string) => {
      const rgb = color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(v => {
        const s = v / 255;
        return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
    };
    const fill = textStyle.webkitTextFillColor || textStyle.color;
    const foreground = luminance(fill);
    const background = luminance(style.backgroundColor);
    return { fill, background: style.backgroundColor,
      ratio: (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05) };
  }, placeholder);
}

for (const theme of ['dark', 'light'] as const) {
  for (const setup of [false, true]) {
    test(`${theme}: ${setup ? 'initial setup' : 'login'} placeholder, bullets and revealed text remain readable`, async ({ page }, testInfo) => {
      await page.addInitScript(({ theme }) => {
        localStorage.setItem('theme', theme);
        localStorage.setItem('guanlan.motion', 'off');
      }, { theme });
      await page.route('**/api/**', async route => {
        const pathname = new URL(route.request().url()).pathname;
        if (!pathname.startsWith('/api/')) return route.continue();
        expect(route.request().method()).toBe('GET'); // Never submit a password to a real backend.
        await route.fulfill({ json: pathname.endsWith('/auth/status')
          ? { authEnabled: true, loggedIn: false, passwordSet: !setup, setupState: setup ? 'no_password' : 'enabled' }
          : {} });
      });
      await page.goto('/login');
      await expect(page.locator('html')).toHaveClass(new RegExp(theme));
      const inputs = setup ? [page.locator('#password'), page.locator('#passwordConfirm')] : [page.locator('#password')];
      for (const input of inputs) {
        await expect(input).toBeVisible();
        expect((await contrast(input, true)).ratio).toBeGreaterThanOrEqual(4.5);
        await input.fill('Visual-test-42'); // Synthetic, never submitted.
        await expect(input).toHaveAttribute('type', 'password');
        expect((await contrast(input)).ratio).toBeGreaterThanOrEqual(4.5);
      }
      for (const [width, height] of [[1440, 900], [390, 844]]) {
        await page.setViewportSize({ width, height });
        for (const input of inputs) expect((await contrast(input)).ratio).toBeGreaterThanOrEqual(4.5);
        await page.screenshot({ path: testInfo.outputPath(`masked-${width}.png`), fullPage: true });
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      }
      await page.getByRole('button', { name: '显示内容', exact: true }).first().click();
      await expect(inputs[0]).toHaveAttribute('type', 'text');
      await expect(inputs[0]).toHaveValue('Visual-test-42');
      expect((await contrast(inputs[0])).ratio).toBeGreaterThanOrEqual(4.5);
      await page.screenshot({ path: testInfo.outputPath('revealed-mobile.png'), fullPage: true });
      await page.getByRole('button', { name: '隐藏内容', exact: true }).click();
      await expect(inputs[0]).toHaveAttribute('type', 'password');
    });
  }
}
