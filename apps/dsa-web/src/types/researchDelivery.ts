export type DestinationKind = 'test' | 'strategy' | 'radar' | 'ops';
export interface DeliveryDestination {
  id: string; alias: string; kind: DestinationKind; enabled: boolean; version: number;
  credential_configured: boolean; signing_configured: boolean; keyword: string;
  created_at: string; updated_at: string;
}
export interface DeliveryCategory {
  category_id: string; display_name: string; description: string; code: string;
  destination_kind: DestinationKind; preview_available: boolean; planned_time: string;
  producer_status: 'historical_draft_only' | 'not_connected' | 'live_on_demand';
}
export interface SubscriptionInput {
  name: string; destination_id: string; strategy_id: string; top_n: number;
  llm_mode: 'off' | 'explain_only'; planned_time: string;
}
export interface DeliverySubscription extends SubscriptionInput {
  id: string; enabled: boolean; version: number; created_at: string; updated_at: string;
}
export interface DeliveryCapabilities {
  management_enabled: boolean; llm_available: boolean; llm_status: string;
  llm_daily_attempt_limit: number; scheduler_active: boolean; manual_test_available: boolean;
  formal_publication_available: boolean; test_message: string; timezone: string;
  sending_enabled?: boolean; global_sending_state?: 'authorized' | 'paused' | 'manual_test_only';
}
export interface DeliverySource {
  source_run_id: string; strategy_id: string; created_at: string; candidate_count: number;
  snapshot_count: number; snapshot_source: string; business_date: string | null; w1_verified: boolean;
}
export interface ResearchPublication {
  format_version?: string;
  title?: string;
  brief_input?: { business_date?: string };
  detail?: { sources?: { source?: string; fetched_at?: string; business_date?: string }[]; audit?: Record<string, unknown> };
  id: string; subscription_id: string; destination_id: string; subscription_version: number;
  destination_version: number; source_run_id: string; content_hash: string; status: string;
  created_at: string; blockers: string[]; rendered_content: string;
  llm: { status: string; reason?: string; usage?: Record<string, number | null>; cost?: number | null };
  envelope?: { w1_verified?: boolean; business_date?: string | null; evidence: { id: string; title: string; url: string | null; published_at: string | null; date_status: string; issuer_status: string }[] };
}
export interface DeliveryRecord {
  id: string; request_id: string; destination_id: string; destination_version: number;
  kind?: 'test' | 'formal' | 'revision' | (string & {}); publication_id?: string | null;
  status: 'pending' | 'sending' | 'sent' | 'rejected' | 'delivery_unknown' | 'suppressed';
  error_code: string | null; attempt_count: number; created_at: string;
}
export interface CredentialInput { version: number; webhook_url: string; signing_secret?: string; keyword?: string }
export interface SetupInput extends Omit<SubscriptionInput, 'destination_id'> {
  request_id: string;
  destination_id?: string;
  destination_version?: number;
  new_destination?: { alias: string; webhook_url: string; signing_secret: string; keyword: string };
}
export interface SetupResult { request_id: string; destination: DeliveryDestination; subscription: DeliverySubscription }

export interface AutomationJob {
  id: string; business_date: string; status: string; stage: string; attempts: number;
  error_code: string | null; batch_id?: string | null; publication_id?: string | null; delivery_id?: string | null;
  coverage: Record<string, string | number | boolean>; updated_at: string;
}
export interface AutomationSubscription {
  subscription_id: string; name: string; category_id: string; destination_id: string;
  planned_time: string; enabled: boolean; job: AutomationJob | null;
}
export interface AutomationStatus {
  enabled: boolean; version: number; worker_alive: boolean; heartbeat_at: string | null;
  timezone: string; collection_time: string; cutoff_time: string; running: boolean;
  subscriptions: AutomationSubscription[]; last_error: string | null;
}
export interface AutomationControl { version: number; enabled: boolean; subscription_ids: string[] }
export interface AutomationRunReceipt {
  request_id: string; business_date: string; authorization_version: number; status: 'accepted';
}
