import apiClient from './index';
import type { CredentialInput, DeliveryCapabilities, DeliveryCategory, DeliveryDestination,
  DeliveryRecord, DeliverySource, DeliverySubscription, DestinationKind, ResearchPublication,
  SubscriptionInput, SetupInput, SetupResult, AutomationStatus, AutomationControl, AutomationRunReceipt } from '../types/researchDelivery';

const base = '/api/v1/research-delivery';
const path = (kind: string, id: string) => `${base}/${kind}/${encodeURIComponent(id)}`;
const list = async <T>(kind: string): Promise<T[]> => (await apiClient.get<{ items: T[] }>(`${base}/${kind}`)).data.items;

/** This feature deliberately keeps the server's snake_case DTOs and write-only secrets. */
export const researchDeliveryApi = {
  automation: async () => (await apiClient.get<AutomationStatus>(`${base}/automation`)).data,
  updateAutomation: async (body: AutomationControl) => (await apiClient.put<AutomationStatus>(`${base}/automation`, body)).data,
  runAutomation: async (body: { request_id: string }) => (await apiClient.post<AutomationRunReceipt>(`${base}/automation/run`, body)).data,
  setup: async (body: SetupInput) => (await apiClient.post<SetupResult>(`${base}/setups`, body)).data,
  setupStatus: async (requestId: string) => (await apiClient.get<SetupResult>(path('setups', requestId))).data,
  capabilities: async () => (await apiClient.get<DeliveryCapabilities>(`${base}/capabilities`)).data,
  categories: () => list<DeliveryCategory>('categories'),
  destinations: () => list<DeliveryDestination>('destinations'),
  subscriptions: () => list<DeliverySubscription>('subscriptions'),
  publications: () => list<ResearchPublication>('publications'),
  deliveries: () => list<DeliveryRecord>('deliveries'),
  createDestination: async (body: { alias: string; kind: DestinationKind }) =>
    (await apiClient.post<DeliveryDestination>(`${base}/destinations`, body)).data,
  updateDestination: async (id: string, body: { version: number; alias?: string; kind?: DestinationKind; enabled?: boolean }) =>
    (await apiClient.patch<DeliveryDestination>(path('destinations', id), body)).data,
  credentials: async (id: string, body: CredentialInput) =>
    (await apiClient.put<DeliveryDestination>(`${path('destinations', id)}/credentials`, body)).data,
  createSubscription: async (body: SubscriptionInput) =>
    (await apiClient.post<DeliverySubscription>(`${base}/subscriptions`, body)).data,
  updateSubscription: async (id: string, body: Partial<SubscriptionInput> & { version: number; enabled?: boolean }) =>
    (await apiClient.patch<DeliverySubscription>(path('subscriptions', id), body)).data,
  sources: async (strategy_id: string) =>
    (await apiClient.get<{ items: DeliverySource[] }>(`${base}/sources`, { params: { strategy_id } })).data.items,
  preview: async (id: string, body: { source_run_id: string; use_llm: boolean }) =>
    (await apiClient.post<ResearchPublication>(`${path('subscriptions', id)}/preview`, body, { timeout: 60000 })).data,
  test: async (id: string, body: { version: number; confirm: true; request_id: string }) =>
    (await apiClient.post<DeliveryRecord>(`${path('destinations', id)}/test`, body)).data,
};
