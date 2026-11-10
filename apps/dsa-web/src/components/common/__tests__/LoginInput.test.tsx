import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Input } from '../Input';

describe('login password visibility control', () => {
  it('keeps the login visibility button in the keyboard tab order and focusable', () => {
    render(<Input id="login-password" label="登录密码" type="password" appearance="login" allowTogglePassword />);

    const toggle = screen.getByRole('button', { name: '显示内容' });
    expect(toggle).toHaveAttribute('type', 'button');
    expect(toggle.tabIndex).toBe(0);
    toggle.focus();
    expect(toggle).toHaveFocus();
  });

  it('toggles visibility without changing credentials or submitting a form', () => {
    render(<Input id="login-password" label="登录密码" type="password" appearance="login" allowTogglePassword defaultValue="local-test-password" />);

    const password = screen.getByLabelText('登录密码');
    fireEvent.click(screen.getByRole('button', { name: '显示内容' }));
    expect(password).toHaveAttribute('type', 'text');
    expect(password).toHaveValue('local-test-password');
    fireEvent.click(screen.getByRole('button', { name: '隐藏内容' }));
    expect(password).toHaveAttribute('type', 'password');
    expect(password).toHaveValue('local-test-password');
  });

  it('disables the login visibility button whenever the credential input is disabled', () => {
    render(<Input id="login-password" label="登录密码" type="password" appearance="login" allowTogglePassword disabled />);

    const password = screen.getByLabelText('登录密码');
    const toggle = screen.getByRole('button', { name: '显示内容' });
    expect(password).toBeDisabled();
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(password).toHaveAttribute('type', 'password');
  });

  it('preserves the default appearance keyboard contract outside the login scope', () => {
    render(<Input id="other-password" label="其他密码" type="password" allowTogglePassword />);

    expect(screen.getByLabelText('其他密码')).toHaveAttribute('data-appearance', 'default');
    expect(screen.getByRole('button', { name: '显示内容' }).tabIndex).toBe(-1);
  });
});
