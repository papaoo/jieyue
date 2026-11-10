import { getParsedApiError } from '../../../api/error';
import type { AutomationStatus, DeliveryRecord, ResearchPublication } from '../../../types/researchDelivery';

export function automationLabel(status: AutomationStatus | null): string {
  return !status ? '自动化状态未确认' : !status.enabled ? '自动推送已暂停'
    : !status.worker_alive ? '自动推送已启用 · 工作进程离线'
    : status.running ? '自动推送已启用 · 正在处理' : '自动推送已启用 · 等待计划';
}

export function safeDeliveryError(error: unknown): string {
  const parsed = getParsedApiError(error);
  const code = (error as { response?: { data?: { error?: string } } })?.response?.data?.error;
  const messages: Record<string, string> = {
    automation_paused: '自动推送总开关已暂停。请刷新运行状态并核对授权，再决定是否启用。（automation_paused）',
    request_id_day_conflict: '此运行编号属于其他业务日。请刷新页面并核对，不要补发历史任务。（request_id_day_conflict）',
    subscription_not_configured: '所选订阅的群连接尚未配置，请先独立保存群连接。（subscription_not_configured）',
    auth_required: '管理员登录尚未开启。请在系统设置中开启密码登录后再保存。（auth_required）',
    cross_origin: '请求来源校验未通过，并非密码错误。请从同一个页面地址访问，避免混用 localhost、IP 或代理入口；仍失败时请提供此代码。（cross_origin）',
    duplicate_subscription: '这个群已经订阅该类别，请在分类列表编辑已有订阅。',
    setup_request_conflict: '保存编号对应的内容不同，请先核对已保存记录，不要覆盖。',
    destination_unconfigured: '该群连接尚未保存，请先到“群连接”补全机器人信息。',
    duplicate_destination: '这个 Webhook 已属于另一个群，请核对每群独立的机器人地址。',
    signing_secret_required: '更换已签名的机器人地址时，必须同时填写新机器人的签名密钥。',
    secure_transport_required: '请通过 HTTPS 或本机回环地址保存凭据；远程 HTTP 连接不能提交机器人密钥。（secure_transport_required）',
    credential_storage_unavailable: '受保护的凭据存储不可用，请检查服务账号和数据目录权限。',
    invalid_webhook: '请填写飞书官方 HTTPS 自定义机器人 Webhook，不能使用 App ID 或 Chat ID。',
    test_keyword_mismatch: '固定测试消息不含该群的关键词，请将机器人关键词设为“阶跃”并同步保存。',
    category_preview_unavailable: '这类内容没有历史运行预览；当日生产能力请查看自动推送状态。',
  };
  if (code && messages[code]) return messages[code];
  if (parsed.status === 401) return '登录已失效，请重新登录管理员账号。';
  if (parsed.status === 403) return '请求被拒绝（403，原因未识别），不能据此判断管理员权限不足。请保留当前草稿并联系维护者核查。';
  if (parsed.status === 409) return '配置或内容已变化。请取消当前编辑并重新读取，核对后再操作；系统不会自动重试。';
  if (parsed.status === 422) return '配置未通过校验，请检查名称、群地址、时间和 Top N。';
  if (parsed.status === 404) return '此记录或多群接口不存在，请刷新页面并确认后端已更新。';
  return '请求未能确认完成。请重新读取记录后核对，不要直接重复提交或发送。';
}

export const deliveryLabels: Record<DeliveryRecord['status'], string> = {
  pending: '等待发送', sending: '发送中', sent: '飞书已接收', rejected: '飞书明确拒绝',
  delivery_unknown: '送达未知', suppressed: '已停止',
};

/** Existing outbox identity, not source readiness or publication status, identifies formal reports. */
export function reportPresentation(report: ResearchPublication, records: DeliveryRecord[]) {
  const linked = records.filter(r => r.publication_id === report.id);
  const delivery = linked.length === 1 ? linked[0] : undefined;
  const historical = report.envelope?.w1_verified === false;
  if (!historical && report.id && report.destination_id && Number.isInteger(report.destination_version) && report.destination_version > 0
      && (delivery?.kind === 'formal' || delivery?.kind === 'revision') && delivery.destination_id === report.destination_id
      && delivery.destination_version === report.destination_version) {
    return { kind: delivery.kind, label: delivery.kind === 'revision' ? '修订版研究报告' : '正式研究报告', accepted: delivery.status === 'sent',
      deliveryLabel: delivery.status === 'sent' ? '飞书已接收（不等于已读）'
        : delivery.status === 'delivery_unknown' ? '送达未知，不自动重发' : deliveryLabels[delivery.status] ?? '投递状态未确认' } as const;
  }
  if (historical && !linked.length) return { kind: 'historical', label: '历史研究草稿', accepted: false, deliveryLabel: '非正式预览 · 禁止发送' } as const;
  return { kind: 'unknown', label: '报告类型未确认', accepted: false, deliveryLabel: '投递状态未确认' } as const;
}

export const groupKinds = [
  { value: 'strategy', label: '策略结果' }, { value: 'radar', label: '资讯 / 风险雷达' },
  { value: 'ops', label: '系统状态' }, { value: 'test', label: '联调测试' },
];

/** Database timestamps are UTC even when the SQLite serializer omits Z. */
export function displayTime(value: string | undefined): string {
  if (!value) return '未知';
  const date = new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.valueOf()) ? '未知' : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

export interface EditorStateProps {
  disabled: boolean;
  onDirtyChange: (dirty: boolean) => void;
}
