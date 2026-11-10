import type { DeliveryRecord } from '../../../types/researchDelivery';
import { deliveryLabels } from './shared';

export function DeliveryNotice({ record }: { record: DeliveryRecord }) {
  return <div role="status" className="rd-notice">
    <strong>{deliveryLabels[record.status] ?? record.status}</strong>
    <p>{record.status === 'delivery_unknown' || record.status === 'sending'
      ? '可能已送达，请先到群里确认；系统不会自动重发。'
      : record.status === 'sent' ? '飞书已确认接收，不代表群成员已读。'
        : '请检查投递记录和该群配置；不会转发到其他群。'}</p>
    <span className="font-mono text-xs">{record.id} · 请求次数 {record.attempt_count}</span>
  </div>;
}
