import { describe, expect, it } from 'vitest';
import { safeDeliveryError } from '../research-delivery/shared';

describe('research delivery permission errors', () => {
  it.each([
    ['auth_required', '管理员登录尚未开启'],
    ['cross_origin', '来源校验未通过'],
    ['secure_transport_required', 'HTTPS 或本机回环地址'],
  ])('explains the actual %s failure instead of mixing three causes', (error, expected) => {
    const message = safeDeliveryError({ response: { status: 403, data: { error, message: 'sensitive-server-detail' } } });
    expect(message).toContain(expected);
    expect(message).not.toContain('sensitive-server-detail');
    expect(message).not.toContain('请先开启管理员登录，并从同源页面访问');
  });

  it('does not claim an unknown 403 proves administrator login is disabled', () => {
    expect(safeDeliveryError({ response: { status: 403, data: { error: 'unknown-code' } } })).toContain('原因未识别');
  });
});
