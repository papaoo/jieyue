import type React from 'react';
import { useState, useEffect } from 'react';
import { motion } from 'motion/react';
import { RiArrowRightUpLine, RiLock2Line, RiPulseLine } from '@remixicon/react';
import { Button, Input } from '../components/common';
import { BrandMark } from '../components/brand/BrandMark';
import { StockMarketVisual } from '../components/brand/StockMarketVisual';
import { UiLanguageToggle } from '../components/i18n/UiLanguageToggle';
import { ThemeToggle } from '../components/theme/ThemeToggle';
import { useNavigate, useSearchParams } from 'react-router-dom';
import type { ParsedApiError } from '../api/error';
import { isParsedApiError } from '../api/error';
import { useAuth } from '../hooks';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import { useMotionPreference } from '../contexts/MotionPreferenceContext';
import { SettingsAlert } from '../components/settings';
import { resolveLoginRedirect } from '../utils/loginRedirect';
import { BRAND } from '../config/brand';
import './LoginPage.css';

const LoginPage: React.FC = () => {
  const { login, passwordSet, setupState } = useAuth();
  const { t } = useUiLanguage();
  const { motionEnabled, toggleMotion } = useMotionPreference();
  const navigate = useNavigate();

  useEffect(() => {
    document.title = t('login.pageTitle');
  }, [t]);
  const [searchParams] = useSearchParams();
  const redirect = resolveLoginRedirect(searchParams.get('redirect'));

  const [password, setPassword] = useState('');
  const [passwordConfirm, setPasswordConfirm] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | ParsedApiError | null>(null);
  const isFirstTime = setupState === 'no_password' || !passwordSet;
  const protocol = window.location.protocol;
  const connectionText = t(protocol === 'https:'
    ? 'login.connectionHttps'
    : protocol === 'http:' ? 'login.connectionHttp' : 'login.connectionOther');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting) return;
    setError(null);
    if (isFirstTime && password !== passwordConfirm) {
      setError(t('login.passwordMismatch'));
      return;
    }
    setIsSubmitting(true);
    try {
      const result = await login(password, isFirstTime ? passwordConfirm : undefined);
      if (result.success) {
        navigate(redirect, { replace: true });
      } else {
        setError(result.error ?? t('login.loginFailed'));
      }
    } catch {
      setError(t('login.loginFailed'));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="jieyue-login" data-login-motion={motionEnabled ? 'on' : 'off'}>
      <div className="jieyue-login__atmosphere" aria-hidden="true" />
      <header className="jieyue-login__header">
        <div className="jieyue-login__wordmark">
          <BrandMark className="jieyue-login__small-mark" />
          <span>{BRAND.name}<small>{BRAND.englishName}</small></span>
        </div>
        <div className="jieyue-login__controls">
          <button
            type="button"
            className="jieyue-login__control"
            onClick={toggleMotion}
            aria-pressed={motionEnabled}
            aria-label={t('login.motionToggle')}
          >
            <RiPulseLine size={16} aria-hidden="true" />
            <span>{motionEnabled ? t('login.motionOn') : t('login.motionOff')}</span>
          </button>
          <ThemeToggle triggerClassName="jieyue-login__control" />
          <UiLanguageToggle triggerClassName="jieyue-login__control" />
        </div>
      </header>

      <div className="jieyue-login__workspace">
        <motion.section
          className="jieyue-login__identity"
          initial={motionEnabled ? { opacity: 0, y: 20 } : false}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: motionEnabled ? 0.8 : 0, ease: [0.22, 1, 0.36, 1] }}
          aria-labelledby="jieyue-brand-title"
        >
          <StockMarketVisual motionEnabled={motionEnabled} caption={t('login.marketMotionStudy')} />
          <div className="jieyue-login__identity-copy">
            <div className="jieyue-login__title-row">
              <h1 id="jieyue-brand-title">{BRAND.name}</h1>
              <span className="jieyue-login__english-name">{BRAND.englishName}<span aria-hidden="true">↗</span></span>
            </div>
            <p className="jieyue-login__tagline">
              {t('login.brandTagline')}
            </p>
          </div>
        </motion.section>

        <motion.section
          className="jieyue-login__panel-shell"
          initial={motionEnabled ? { opacity: 0, y: 24 } : false}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: motionEnabled ? 0.75 : 0, delay: motionEnabled ? 0.12 : 0, ease: [0.22, 1, 0.36, 1] }}
          aria-labelledby="jieyue-login-title"
        >
          <div className="jieyue-login__panel">
          <div className="jieyue-login__panel-header">
            <div className="jieyue-login__access-icon"><RiLock2Line size={21} aria-hidden="true" /></div>
            <h2 id="jieyue-login-title">{isFirstTime ? t('login.setupTitle') : t('login.adminLogin')}</h2>
            <p>{isFirstTime ? t('login.setupDescription') : t('login.loginDescription')}</p>
          </div>

          <form onSubmit={handleSubmit} className="jieyue-login__form" aria-busy={isSubmitting}>
            <Input
              id="password"
              type="password"
              appearance="login"
              allowTogglePassword
              iconType="password"
              label={isFirstTime ? t('login.adminPassword') : t('login.loginPassword')}
              placeholder={isFirstTime ? t('login.setupPasswordPlaceholder') : t('login.loginPasswordPlaceholder')}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={isSubmitting}
              autoFocus
              autoComplete={isFirstTime ? 'new-password' : 'current-password'}
            />
            {isFirstTime && (
              <Input
                id="passwordConfirm"
                type="password"
                appearance="login"
                allowTogglePassword
                iconType="password"
                label={t('login.confirmPassword')}
                placeholder={t('login.confirmPasswordPlaceholder')}
                value={passwordConfirm}
                onChange={(e) => setPasswordConfirm(e.target.value)}
                disabled={isSubmitting}
                autoComplete="new-password"
              />
            )}
            {error && (
              <div role="alert" className="jieyue-login__error">
                <SettingsAlert
                  title={isFirstTime ? t('login.setupFailed') : t('login.validationFailed')}
                  message={isParsedApiError(error) ? error.message : error}
                  variant="error"
                />
              </div>
            )}
            <Button
              type="submit"
              variant="primary"
              size="lg"
              className="jieyue-login__submit"
              disabled={isSubmitting}
              aria-busy={isSubmitting}
            >
              <span>{isSubmitting
                ? isFirstTime ? t('login.setupSubmitting') : t('login.loginSubmitting')
                : isFirstTime ? t('login.setupSubmit') : t('login.loginSubmit')}</span>
              <span className="jieyue-login__submit-icon" aria-hidden="true">
                {isSubmitting ? <span className="jieyue-login__progress-mark" /> : <RiArrowRightUpLine size={21} />}
              </span>
            </Button>
          </form>
          <div className="jieyue-login__connection" data-protocol={protocol}>
            <RiLock2Line size={14} aria-hidden="true" /><p>{connectionText}</p>
          </div>
          </div>
        </motion.section>
      </div>

      <footer className="jieyue-login__footer">
        <span>{t('login.researchNotice')}</span>
        <span className="jieyue-login__footer-signature">{BRAND.englishName}</span>
      </footer>
    </main>
  );
};

export default LoginPage;
