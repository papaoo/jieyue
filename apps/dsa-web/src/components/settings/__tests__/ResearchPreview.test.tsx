import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ResearchPreview } from '../research-delivery/ResearchPreview';
import type { DeliveryCapabilities, DeliveryRecord, ResearchPublication } from '../../../types/researchDelivery';

const publication = (extra: Partial<ResearchPublication> = {}): ResearchPublication => ({
  id: 'p1', subscription_id: 's1', destination_id: 'g1', destination_version: 2, subscription_version: 1,
  source_run_id: 'run1', content_hash: 'hash', status: 'prepared', created_at: '2026-10-15T09:10:00',
  blockers: [], rendered_content: '冻结内容', llm: { status: 'off' }, ...extra,
});
const delivery = (extra: Partial<DeliveryRecord> = {}): DeliveryRecord => ({
  id: 'd1', request_id: 'r1', kind: 'formal', publication_id: 'p1', destination_id: 'g1', destination_version: 2,
  status: 'pending', error_code: null, attempt_count: 0, created_at: '2026-10-15T09:10:00', ...extra,
});
function mount(report: ResearchPublication, records: DeliveryRecord[] = []) {
  render(<ResearchPreview subscriptions={[]} groups={[]} categories={[]} capabilities={{ llm_available: true } as DeliveryCapabilities}
    publications={[report]} records={records} disabled={false} onPreview={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: /历史订阅/ }));
  return within(screen.getByRole('article'));
}
describe('frozen report identity and delivery acknowledgement', () => {
  it('distinguishes a uniquely associated revision receipt from the formal original', () => {
    const report = mount(publication(), [delivery({ kind: 'revision', status: 'sent' })]);
    expect(report.getByText('修订版研究报告')).toBeInTheDocument();
    expect(report.queryByText('正式研究报告')).not.toBeInTheDocument();
    expect(report.getByText('飞书已接收（不等于已读）')).toBeInTheDocument();
    expect(report.queryByRole('button', { name: /发送|发布/ })).not.toBeInTheDocument();
  });
  it('keeps incomplete revision reservations neutral and readable', () => {
    const report = mount(publication({ status: 'preparing', blockers: ['revision_preparing'],
      rendered_content: '修订版准备中，尚不可发送。', llm: { status: 'not_requested' } }));
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.getByText('修订版准备中，尚不可发送。')).toBeInTheDocument();
  });
  it('bounds 200 observation timestamps to a visible range and keeps every timestamp in collapsed audit', () => {
    const times = Array.from({ length: 200 }, (_, i) => new Date(Date.UTC(2026, 9, 12, 9, 0, i)).toISOString());
    const frozenBody = '冻结正文\n\n条件观察：只核验原始事实。';
    const report = mount(publication({ rendered_content: frozenBody,
      detail: { sources: times.map(fetched_at => ({ fetched_at, source: 'tushare' })) } }));
    const summary = report.getByText(/观测时间（北京时间）/);
    expect(summary.textContent?.length).toBeLessThan(150);
    expect(summary).toHaveTextContent('200 个时间点');
    expect(summary).toHaveTextContent(/至/);
    const audit = report.getByText('核验信息与用量').closest('details');
    expect(audit).not.toHaveAttribute('open');
    expect(audit?.textContent).toContain(times[0]);
    expect(audit?.textContent).toContain(times[100]);
    expect(audit?.textContent).toContain(times[199]);
    expect(report.getByText(/冻结正文/).textContent).toBe(frozenBody);
  });
  it('shows the new frozen brief with data/observation dates and collapsed audit without a local detail link', () => {
    const report = mount(publication({ format_version: 'research-brief-v1', title: '研究简报',
      brief_input: { business_date: '2026-10-12' },
      detail: { sources: [{ fetched_at: '2026-10-12T17:10:00+08:00', source: 'tushare' }], audit: { coverage: { valid_count: 100 } } },
      rendered_content: '示例企业（600001）\n\n条件观察：后续核验量价。',
    }));
    expect(report.getByText(/数据日期：2026-10-12/)).toBeInTheDocument();
    expect(report.getByText(/历史数据/)).toBeInTheDocument();
    expect(report.getByText(/观测时间/)).toBeInTheDocument();
    expect(report.getByText(/示例企业/)).toBeInTheDocument();
    const audit = report.getByText('核验信息与用量').closest('details');
    expect(audit).not.toHaveAttribute('open');
    expect(report.queryByRole('link', { name: /完整报告/ })?.getAttribute('href') ?? '').not.toMatch(/127\.0\.0\.1|localhost/);
  });
  it('keeps old text-only content readable when additive brief metadata is absent', () => {
    const report = mount(publication({ rendered_content: '旧版冻结研究正文' }));
    expect(report.getByText('旧版冻结研究正文')).toBeInTheDocument();
    expect(report.getByText(/数据日期.*未知/)).toBeInTheDocument();
  });
  it('keeps explicit historical drafts nonpublishable', () => {
    const report = mount(publication({ status: 'blocked', envelope: { w1_verified: false, evidence: [] } }));
    expect(report.getByText('历史研究草稿')).toBeInTheDocument();
    expect(report.getByRole('button', { name: '历史草稿不可正式发布' })).toBeDisabled();
    expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it.each([
    ['pending', '等待发送'], ['sending', '发送中'], ['sent', '飞书已接收（不等于已读）'],
    ['delivery_unknown', '送达未知，不自动重发'], ['rejected', '飞书明确拒绝'], ['suppressed', '已停止'],
  ] as const)('uses the linked formal delivery acknowledgement for %s', (status, label) => {
    const report = mount(publication(), [delivery({ status })]);
    expect(report.getByText('正式研究报告')).toBeInTheDocument();
    expect(report.getByText(label)).toBeInTheDocument();
    expect(report.queryByText(/待核验|历史草稿|禁止正式发布/)).not.toBeInTheDocument();
    expect(report.queryByRole('button', { name: /发送|发布/ })).not.toBeInTheDocument();
    if (status !== 'sent') expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it.each(['prepared', 'ready', 'sent'])('does not infer identity or acceptance from publication status %s', status => {
    const report = mount(publication({ status }));
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.getByText('投递状态未确认')).toBeInTheDocument();
    expect(report.queryByText(/飞书已接收|正式研究报告|待核验/)).not.toBeInTheDocument();
    expect(report.queryByRole('button', { name: /发送|发布/ })).not.toBeInTheDocument();
  });
  it.each([
    { publication_id: 'other' }, { destination_id: 'other' }, { destination_version: 3 },
    { kind: 'test' }, { kind: undefined },
  ])('ignores mismatched or unidentified delivery records %j', extra => {
    const report = mount(publication(), [delivery({ status: 'sent', ...extra })]);
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it('treats contradictory historical/formal provenance as unknown', () => {
    const report = mount(publication({ envelope: { w1_verified: false, evidence: [] } }), [delivery({ status: 'sent' })]);
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it('does not pick an acknowledgement when multiple formal records match', () => {
    const report = mount(publication(), [delivery({ status: 'sent' }), delivery({ id: 'd2', status: 'delivery_unknown' })]);
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it('keeps source verification without a delivery identity neutral', () => {
    const report = mount(publication({ status: 'ready', envelope: { w1_verified: true, evidence: [] } }));
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
  it('does not treat two missing target versions as a verified identity match', () => {
    const report = mount(publication({ destination_version: undefined }), [delivery({ status: 'sent', destination_version: undefined })]);
    expect(report.getByText('报告类型未确认')).toBeInTheDocument();
    expect(report.queryByText(/飞书已接收/)).not.toBeInTheDocument();
  });
});
