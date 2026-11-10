import { expect, test, type Page } from '@playwright/test';

const strategyNames = ['放量突破', '缩量回踩', '趋势质量', '资金热度', '超跌反转', '均衡多因子', '双低选股', '稳健价值', '低波质量', '蓝筹收益质量'];
const strategyIds = ['volume_breakout', 'shrink_pullback', 'momentum_quality', 'capital_heat', 'oversold_reversal', 'balanced_alpha', 'dual_low', 'quality_value', 'low_volatility_quality', 'blue_chip_income'];
const categories = [...strategyNames.map((display_name, i) => ({ category_id: strategyIds[i], display_name, code: `S${String(i + 1).padStart(2, '0')}`, destination_kind: 'strategy', producer_status: 'live_on_demand', preview_available: true, planned_time: `17:${String(10 + i * 5).padStart(2, '0')}`, description: '复用原始规则和排序，缺失证据保留。' })),
  ...['ETF 轮动', '市场雷达', '公告与风险', '系统状态'].map((display_name, i) => ({ category_id: ['etf_rotation', 'market_radar', 'announcement_risk', 'system_status'][i], display_name, code: ['S11', 'R01', 'R02', 'O01'][i], destination_kind: i === 0 ? 'strategy' : i === 3 ? 'ops' : 'radar', producer_status: i === 3 ? 'not_connected' : 'live_on_demand', preview_available: false, planned_time: ['18:00', '18:10', '20:30', '19:00'][i], description: i === 3 ? '可配置独立群；内容尚未接入。' : '已接入当日有明确范围的数据生产。' }))];
type Row = { id: string; alias: string; kind: string; enabled: boolean; version: number; credential_configured: boolean; signing_configured: boolean; keyword: string };
async function fixture(page: Page, existing = false, setupOutcome: 'ok' | 'rejected' | 'lost' = 'ok') {
  const groups: Row[] = existing ? [{ id: 'g1', alias: '阶跃｜S01 放量突破', kind: 'strategy', enabled: true, version: 3, credential_configured: true, signing_configured: true, keyword: '阶跃' }] : [];
  const subscriptions: Record<string, unknown>[] = [];
  const records: Record<string, unknown>[] = [];
  const writes: string[] = [];
  const credentialBodies: Record<string, unknown>[] = [];
  const setups = new Map<string, unknown>();
  let automationEnabled = false, automationVersion = 1;
  const automation = () => ({ enabled: automationEnabled, version: automationVersion, worker_alive: true, heartbeat_at: '2026-10-15T09:05:00', running: false, timezone: 'Asia/Shanghai', collection_time: '17:05', cutoff_time: '22:30', subscriptions: subscriptions.map(s => ({ subscription_id: s.id, name: s.name, category_id: s.strategy_id, destination_id: s.destination_id, planned_time: s.planned_time, enabled: s.enabled, job: null })), last_error: null });
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (url.origin !== 'http://127.0.0.1:4173') { await route.abort(); return; }
    if (!path.startsWith('/api/')) { await route.continue(); return; }
    const method = request.method();
    if (method !== 'GET') writes.push(`${method} ${path}`);
    const prefix = '/api/v1/research-delivery';
    let json: unknown;
    if (path.startsWith(prefix)) {
      const tail = path.slice(prefix.length), body = method !== 'GET' ? request.postDataJSON() as Record<string, unknown> : {};
      if (tail === '/automation' && method === 'GET') json = automation();
      else if (tail === '/automation' && method === 'PUT') {
        expect(body.version).toBe(automationVersion);
        if (automationEnabled !== Boolean(body.enabled)) automationVersion++;
        automationEnabled = Boolean(body.enabled);
        for (const s of subscriptions) if ((body.subscription_ids as string[]).includes(String(s.id)) && !s.enabled) { s.enabled = true; s.version = Number(s.version) + 1; }
        json = automation();
      } else if (tail === '/automation/run' && method === 'POST') json = { request_id: body.request_id, business_date: '2026-10-15', authorization_version: automationVersion, status: 'accepted' };
      else if (tail === '/setups' && method === 'POST') {
        if (setupOutcome === 'rejected') { await route.fulfill({ status: 422, json: { error: 'invalid_webhook' } }); return; }
        const connection = body.new_destination as Record<string, unknown> | undefined;
        const destination = connection ? { id: `g${groups.length + 1}`, alias: String(connection.alias), kind: 'strategy', version: 1, enabled: false, credential_configured: true, signing_configured: !!connection.signing_secret, keyword: String(connection.keyword), updated_at: '2026-10-15T08:00:00', created_at: '2026-10-15T08:00:00' } : groups.find(g => g.id === body.destination_id)!;
        if (connection) { groups.push(destination); credentialBodies.push(connection); }
        const subscription = { id: `s${subscriptions.length + 1}`, name: body.name, strategy_id: body.strategy_id, destination_id: destination.id, top_n: body.top_n, llm_mode: body.llm_mode, planned_time: body.planned_time, enabled: false, version: 1, updated_at: '2026-10-15T08:00:00', created_at: '2026-10-15T08:00:00' };
        subscriptions.push(subscription); json = { request_id: body.request_id, destination, subscription }; setups.set(String(body.request_id), json);
        if (setupOutcome === 'lost') { await route.abort(); return; }
      } else if (tail.startsWith('/setups/') && method === 'GET') json = setups.get(tail.split('/')[2]);
      else if (tail === '/capabilities') json = { llm_available: true, llm_daily_attempt_limit: 20, scheduler_active: true, sending_enabled: automationEnabled, manual_test_available: true, formal_publication_available: true, test_message: '阶跃 · 群连接测试\n固定非金融测试，不自动重发。' };
      else if (tail === '/categories') json = { items: categories };
      else if (tail === '/destinations' && method === 'GET') json = { items: groups };
      else if (tail === '/destinations' && method === 'POST') {
        const row: Row = { id: `g${groups.length + 1}`, alias: String(body.alias), kind: String(body.kind), enabled: false, version: 1, credential_configured: false, signing_configured: false, keyword: '阶跃' };
        groups.push(row); json = row;
      } else if (/^\/destinations\/[^/]+(?:\/credentials)?$/.test(tail)) {
        const row = groups.find(g => g.id === tail.split('/')[2])!;
        expect(body.version).toBe(row.version);
        if (tail.endsWith('/credentials')) { credentialBodies.push(body); row.credential_configured = true; row.signing_configured = !!body.signing_secret; }
        else if ('enabled' in body) row.enabled = Boolean(body.enabled);
        else { row.alias = String(body.alias); row.kind = String(body.kind); }
        row.version++; json = row;
      } else if (tail === '/subscriptions' && method === 'GET') json = { items: subscriptions };
      else if (tail === '/subscriptions' && method === 'POST') { const row = { ...body, id: `s${subscriptions.length + 1}`, version: 1, enabled: false }; subscriptions.push(row); json = row; }
      else if (tail === '/deliveries') json = { items: records };
      else if (tail === '/publications' || tail === '/sources') json = { items: [] };
      else if (tail.endsWith('/test')) {
        expect(body.confirm).toBe(true);
        const row = { id: 'delivery1', request_id: body.request_id, destination_id: tail.split('/')[2], destination_version: body.version, status: 'delivery_unknown', attempt_count: 1, created_at: '2026-10-15T06:00:00' };
        records.push(row); json = row;
      } else { throw new Error(`Unexpected research API: ${method} ${tail}`); }
    } else if (path === '/api/v1/auth/status') json = { authEnabled: true, loggedIn: true, passwordSet: true, setupState: 'enabled' };
    else if (path === '/api/v1/system/config') json = { configVersion: 'fixture-v1', maskToken: '******', items: [
      { key: 'FIXTURE_VALUE', value: 'saved', rawValueExists: true, isMasked: false, schema: { key: 'FIXTURE_VALUE', title: 'Fixture value', category: 'base', dataType: 'string', uiControl: 'text', isSensitive: false, isEditable: true, isRequired: false, options: [], validation: {}, displayOrder: 1 } },
      { key: 'FEISHU_WEBHOOK_URL', value: '******', rawValueExists: true, isMasked: true, schema: { key: 'FEISHU_WEBHOOK_URL', title: '旧版单群地址', category: 'notification', dataType: 'string', uiControl: 'password', isSensitive: true, isEditable: true, isRequired: false, options: [], validation: {}, displayOrder: 1 } }] };
    else if (path === '/api/v1/system/config/setup/status') json = { ready: true, checks: [] };
    else { expect(method).toBe('GET'); json = { success: true, items: [], accounts: [], total: 0 }; }
    await route.fulfill({ json });
  });
  await page.goto('/settings');
  await page.getByRole('navigation', { name: '配置分类' }).getByRole('button', { name: /通知渠道/ }).click();
  await expect(page.getByRole('heading', { name: '多群研究推送', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '群连接', exact: true })).toBeVisible();
  return { groups, subscriptions, records, writes, credentialBodies };
}

test('page creates two separate groups, binds categories, and saves without sending', async ({ page }, testInfo) => {
  const state = await fixture(page);
  const panel = page.locator('.research-delivery');
  await expect(page.getByRole('button', { name: /^保存配置/ })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '通知测试', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('飞书群机器人 Webhook', { exact: true })).toHaveCount(0);
  for (const [index, title] of ['突破研究群', '回踩研究群'].entries()) {
    await panel.getByRole('button', { name: `S0${index + 1} ${strategyNames[index]}`, exact: true }).click();
    await panel.getByLabel('飞书群名称', { exact: true }).fill(title);
    await panel.getByLabel('该群 Webhook').fill(`https://open.feishu.cn/open-apis/bot/v2/hook/fixture-${index}`);
    await panel.getByLabel('该群签名密钥（可选）').fill(`fixture-signature-${index}`);
    await panel.getByRole('button', { name: '保存群订阅' }).click();
    await expect(panel.getByRole('status')).toContainText(`→ ${title}`);
    await expect(panel.getByLabel('该群 Webhook')).toHaveCount(0);
    await expect(panel.getByRole('button', { name: '保存该类订阅' })).toBeDisabled();
  }
  expect(state.subscriptions.map(s => s.destination_id)).toEqual(['g1', 'g2']);
  expect(state.credentialBodies[0].webhook_url).not.toBe(state.credentialBodies[1].webhook_url);
  expect(state.writes.some(w => /\/test|\/preview|\/send/.test(w))).toBe(false);
  expect(state.writes).toEqual(['POST /api/v1/research-delivery/setups', 'POST /api/v1/research-delivery/setups']);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toMatch(/fixture-signature|fixture-0|fixture-1/);
  await page.reload();
  await page.getByRole('navigation', { name: '配置分类' }).getByRole('button', { name: /通知渠道/ }).click();
  await expect(panel.getByRole('button', { name: 'S01 放量突破', exact: true })).toContainText('突破研究群');
  await expect(panel.getByRole('button', { name: 'S02 缩量回踩', exact: true })).toContainText('回踩研究群');
  for (const [name, width, height, theme] of [['desktop-dark', 1440, 900, 'dark'], ['mobile-light', 390, 844, 'light']] as const) {
    await page.setViewportSize({ width, height });
    await page.evaluate(t => document.documentElement.classList.toggle('dark', t === 'dark'), theme);
    await panel.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true, animations: 'disabled' });
  }
  await panel.getByRole('button', { name: '新增群订阅' }).click();
  await panel.getByLabel('内容类别').scrollIntoViewIfNeeded();
  await expect(panel.getByLabel('内容类别')).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('mobile-setup.png'), fullPage: true, animations: 'disabled' });
});

test('each target test needs confirmation; unknown cannot auto-repeat', async ({ page }, testInfo) => {
  const state = await fixture(page, true);
  const panel = page.locator('.research-delivery');
  await panel.getByRole('button', { name: '群连接', exact: true }).click();
  await panel.getByRole('button', { name: '测试连接 阶跃｜S01 放量突破' }).click();
  const confirm = panel.getByRole('region', { name: '确认向此群发测试' });
  await expect(confirm).toBeFocused();
  expect(state.writes).toHaveLength(0);
  await confirm.getByRole('button', { name: '取消，不发送' }).click();
  await expect(panel.getByRole('button', { name: '测试连接 阶跃｜S01 放量突破' })).toBeFocused();
  await panel.getByRole('button', { name: '测试连接 阶跃｜S01 放量突破' }).click();
  await confirm.getByRole('button', { name: '确认发送这一条' }).click();
  await expect(confirm.getByText(/可能已送达/)).toBeVisible();
  await expect(confirm.getByRole('button', { name: '确认发送这一条' })).toBeDisabled();
  await confirm.screenshot({ path: testInfo.outputPath('unknown-delivery.png') });
  await confirm.getByRole('button', { name: '关闭并核对记录' }).click();
  await expect(panel.getByRole('button', { name: '测试连接 阶跃｜S01 放量突破' })).toBeDisabled();
  expect(state.writes.filter(w => w.endsWith('/test'))).toHaveLength(1);
});

test('group draft survives settings-category change and blocks leaving until discarded', async ({ page }) => {
  await fixture(page);
  const panel = page.locator('.research-delivery');
  await panel.getByRole('button', { name: '新增群订阅' }).click();
  await panel.getByLabel('飞书群名称').fill('未保存群');
  await page.getByRole('button', { name: '其他渠道与旧版通知', exact: true }).click();
  await expect(panel).toBeHidden();
  await expect(page.getByRole('heading', { name: '通知测试', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '飞书多群订阅', exact: true }).click();
  await expect(panel.getByLabel('飞书群名称')).toHaveValue('未保存群');
  const nav = page.getByRole('navigation', { name: '配置分类' });
  await nav.getByRole('button', { name: /基础设置/ }).click();
  await page.getByRole('link', { name: '告警', exact: true }).click();
  await expect(page.getByRole('heading', { name: '存在未保存的修改' })).toBeVisible();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await nav.getByRole('button', { name: /通知渠道/ }).click();
  await expect(panel.getByLabel('飞书群名称')).toHaveValue('未保存群');
  await page.getByRole('button', { name: '放弃未保存草稿', exact: true }).click();
  await expect(panel.getByLabel('飞书群名称')).toHaveCount(0);
  await page.getByRole('link', { name: '告警', exact: true }).click();
  await expect(page).toHaveURL(/\/alerts$/);
});

test('lost save response is recovered by lookup without duplicate group creation', async ({ page }) => {
  const state = await fixture(page, false, 'lost');
  const panel = page.locator('.research-delivery');
  await panel.getByRole('button', { name: 'S01 放量突破', exact: true }).click();
  await panel.getByLabel('飞书群名称').fill('已提交待核对群');
  await panel.getByLabel('该群 Webhook').fill('https://open.feishu.cn/open-apis/bot/v2/hook/fixture-lost');
  await panel.getByRole('button', { name: '保存群订阅' }).click();
  await expect(panel.getByRole('alert')).toContainText('尚未确认');
  await expect(panel.getByLabel('飞书群名称')).toBeDisabled();
  await panel.getByRole('button', { name: '核对是否已保存' }).click();
  await expect(panel.getByRole('status')).toContainText('已提交待核对群');
  expect(state.groups).toHaveLength(1); expect(state.subscriptions).toHaveLength(1); expect(state.writes).toHaveLength(1);
});

test('rejected setup keeps entered fields and does not create a partial group', async ({ page }) => {
  const state = await fixture(page, false, 'rejected');
  const panel = page.locator('.research-delivery');
  await panel.getByRole('button', { name: 'S01 放量突破', exact: true }).click();
  await panel.getByLabel('飞书群名称').fill('失败保留草稿');
  await panel.getByLabel('该群 Webhook').fill('https://invalid.example/fixture');
  await panel.getByRole('button', { name: '保存群订阅' }).click();
  await expect(panel.getByRole('alert')).toContainText('未保存');
  await expect(panel.getByLabel('飞书群名称')).toHaveValue('失败保留草稿');
  await expect(panel.getByLabel('该群 Webhook')).toBeEnabled();
  expect(state.groups).toHaveLength(0); expect(state.subscriptions).toHaveLength(0);
});
