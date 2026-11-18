import type {
  AuthSession,
  BillingStatus,
  Catalog,
  Campaign,
  InvoiceInfo,
  MyOrderSummary,
  NotificationStatus,
  Order,
  OrderLine,
  Product
} from './types';

const defaultTimeoutMs = 12000;

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: unknown;

  constructor(code: string, status: number, message: string, details: unknown = null) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  timeoutMs?: number;
}

interface RawResponse {
  payload: unknown;
  status: number;
}

interface FriendlyError {
  code: string;
  message: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && Number.isFinite(value);
}

function isNotificationStatus(value: unknown): value is NotificationStatus {
  return value === 'pending' || value === 'sent' || value === 'failed';
}

function invalidResponse(): ApiError {
  return new ApiError('RESPONSE_INVALID', 0, 'La respuesta del servidor no es válida.');
}

function readString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== 'string' || value.length === 0) {
    throw invalidResponse();
  }
  return value;
}

function readInteger(record: Record<string, unknown>, key: string, minimum: number): number {
  const value = record[key];
  if (!isInteger(value) || value < minimum) {
    throw invalidResponse();
  }
  return value;
}

function parseProduct(value: unknown): Product {
  if (!isRecord(value)) {
    throw invalidResponse();
  }
  const image = readString(value, 'image');
  if (!/^\/assets\/[a-z0-9-]+\.svg$/i.test(image)) {
    throw invalidResponse();
  }
  return {
    id: readString(value, 'id'),
    name: readString(value, 'name'),
    description: readString(value, 'description'),
    category: readString(value, 'category'),
    price_cents: readInteger(value, 'price_cents', 0),
    original_price_cents: readInteger(value, 'original_price_cents', 0),
    stock: readInteger(value, 'stock', 0),
    image
  };
}

function parseCampaign(value: unknown): Campaign {
  if (!isRecord(value)) {
    throw invalidResponse();
  }
  const endsAt = readString(value, 'ends_at');
  if (Number.isNaN(new Date(endsAt).getTime())) {
    throw invalidResponse();
  }
  return {
    name: readString(value, 'name'),
    ends_at: endsAt,
    currency: readString(value, 'currency')
  };
}

function parseCatalog(value: unknown): Catalog {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw invalidResponse();
  }
  const items = value.items.map(parseProduct);
  const ids = new Set(items.map((item) => item.id));
  if (ids.size !== items.length) {
    throw invalidResponse();
  }
  return {
    items,
    campaign: parseCampaign(value.campaign)
  };
}

function parseOrderLine(value: unknown): OrderLine {
  if (!isRecord(value)) {
    throw invalidResponse();
  }
  return {
    product_id: readString(value, 'product_id'),
    name: readString(value, 'name'),
    quantity: readInteger(value, 'quantity', 1),
    unit_price_cents: readInteger(value, 'unit_price_cents', 0)
  };
}

function parseOrder(value: unknown): Order {
  if (!isRecord(value)) {
    throw invalidResponse();
  }
  if (value.status !== 'confirmed' || !isNotificationStatus(value.notification_status)) {
    throw invalidResponse();
  }
  if (!Array.isArray(value.items) || value.items.length === 0) {
    throw invalidResponse();
  }
  const createdAt = readString(value, 'created_at');
  if (Number.isNaN(new Date(createdAt).getTime())) {
    throw invalidResponse();
  }
  return {
    id: readString(value, 'id'),
    status: 'confirmed',
    notification_status: value.notification_status,
    items: value.items.map(parseOrderLine),
    total_cents: readInteger(value, 'total_cents', 0),
    created_at: createdAt
  };
}

async function request(path: string, options: RequestOptions = {}): Promise<RawResponse> {
  const controller = new AbortController();
  const externalSignal = options.signal;
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeoutMs ?? defaultTimeoutMs);

  const forwardAbort = (): void => {
    controller.abort();
  };

  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener('abort', forwardAbort, { once: true });
    }
  }

  try {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      ...options.headers
    };
    const body = options.body === undefined ? undefined : JSON.stringify(options.body);
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }

    const response = await fetch(path, {
      method: options.method ?? 'GET',
      headers,
      body,
      cache: 'no-store',
      credentials: 'same-origin',
      mode: 'same-origin',
      signal: controller.signal
    });

    const text = await response.text();
    let payload: unknown = null;

    if (text.length > 0) {
      try {
        payload = JSON.parse(text) as unknown;
      } catch {
        throw new ApiError('RESPONSE_INVALID', response.status, 'La respuesta del servidor no es válida.');
      }
    }

    if (!response.ok) {
      if (isRecord(payload) && isRecord(payload.error)) {
        const error = payload.error;
        const code = typeof error.code === 'string' ? error.code : 'HTTP_ERROR';
        const message =
          typeof error.message === 'string' ? error.message : 'No se pudo completar la solicitud.';
        throw new ApiError(code, response.status, message, error.details ?? null);
      }
      throw new ApiError(
        'HTTP_ERROR',
        response.status,
        'No se pudo completar la solicitud.',
        null
      );
    }

    if (text.length === 0) {
      throw new ApiError('RESPONSE_INVALID', response.status, 'La respuesta del servidor está vacía.');
    }

    return { payload, status: response.status };
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (externalSignal?.aborted) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw error;
      }
      throw new DOMException('Aborted', 'AbortError');
    }
    if (timedOut || controller.signal.aborted) {
      throw new ApiError('NETWORK_TIMEOUT', 0, 'La solicitud tardó demasiado.', null);
    }
    throw new ApiError('NETWORK_ERROR', 0, 'No se pudo conectar con el servidor.', null);
  } finally {
    window.clearTimeout(timer);
    externalSignal?.removeEventListener('abort', forwardAbort);
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

export async function fetchCatalog(signal?: AbortSignal): Promise<Catalog> {
  const response = await request('/api/v1/products', { signal });
  return parseCatalog(response.payload);
}

export async function createOrder(
  items: Array<{ product_id: string; quantity: number }>,
  idempotencyKey: string,
  signal?: AbortSignal,
  accessToken?: string | null
): Promise<Order> {
  const headers: Record<string, string> = { 'Idempotency-Key': idempotencyKey };
  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken}`;
  }
  const response = await request('/api/v1/orders', {
    method: 'POST',
    body: { items },
    headers,
    signal
  });
  if (response.status !== 201) {
    throw new ApiError(
      'RESPONSE_INVALID',
      response.status,
      'El servidor no confirmó el pedido con el estado esperado.',
      null
    );
  }
  return parseOrder(response.payload);
}

export async function fetchOrder(id: string, signal?: AbortSignal): Promise<Order> {
  const response = await request(`/api/v1/orders/${encodeURIComponent(id)}`, { signal });
  return parseOrder(response.payload);
}

// ---- Login de clientes (Supabase Auth a través del backend S09) ----

function parseSession(value: unknown): AuthSession {
  if (
    !isRecord(value) ||
    typeof value.access_token !== 'string' ||
    typeof value.refresh_token !== 'string' ||
    typeof value.expires_at !== 'number' ||
    !isRecord(value.user) ||
    typeof value.user.id !== 'string' ||
    typeof value.user.email !== 'string'
  ) {
    throw invalidResponse();
  }
  return {
    access_token: value.access_token,
    refresh_token: value.refresh_token,
    expires_at: value.expires_at,
    user: { id: value.user.id, email: value.user.email }
  };
}

export async function fetchAuthEnabled(signal?: AbortSignal): Promise<boolean> {
  const response = await request('/api/v1/auth/status', { signal });
  return isRecord(response.payload) && response.payload.enabled === true;
}

export async function signIn(email: string, password: string): Promise<AuthSession> {
  const response = await request('/api/v1/auth/login', {
    method: 'POST',
    body: { email, password }
  });
  return parseSession(response.payload);
}

/** Devuelve la sesión, o null si Supabase exige confirmar el correo primero. */
export async function signUp(email: string, password: string): Promise<AuthSession | null> {
  const response = await request('/api/v1/auth/signup', {
    method: 'POST',
    body: { email, password }
  });
  if (response.status === 202) {
    return null;
  }
  return parseSession(response.payload);
}

export async function refreshAuthSession(refreshToken: string): Promise<AuthSession> {
  const response = await request('/api/v1/auth/refresh', {
    method: 'POST',
    body: { refresh_token: refreshToken }
  });
  return parseSession(response.payload);
}

export async function signOut(accessToken: string): Promise<void> {
  try {
    await fetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${accessToken}` },
      credentials: 'same-origin',
      mode: 'same-origin',
      cache: 'no-store'
    });
  } catch {
    // Se cierra la sesión localmente aunque la red falle.
  }
}

export async function fetchMyOrders(accessToken: string, signal?: AbortSignal): Promise<MyOrderSummary[]> {
  const response = await request('/api/v1/me/orders', {
    headers: { Authorization: `Bearer ${accessToken}` },
    signal
  });
  const value = response.payload;
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw invalidResponse();
  }
  return value.items.map((item: unknown): MyOrderSummary => {
    if (
      !isRecord(item) ||
      !isNotificationStatus(item.notification_status) ||
      !billingStatuses.includes(item.billing_status as BillingStatus)
    ) {
      throw invalidResponse();
    }
    return {
      id: readString(item, 'id'),
      created_at: readString(item, 'created_at'),
      total_cents: readInteger(item, 'total_cents', 0),
      units: readInteger(item, 'units', 0),
      notification_status: item.notification_status,
      billing_status: item.billing_status as BillingStatus,
      invoice_number: readNullableString(item, 'invoice_number')
    };
  });
}

export function friendlyAuthError(error: unknown): string {
  const code = error instanceof ApiError ? error.code : 'UNKNOWN';
  switch (code) {
    case 'INVALID_CREDENTIALS':
      return 'Correo o contraseña incorrectos.';
    case 'EMAIL_NOT_CONFIRMED':
      return 'Primero confirma tu correo con el enlace que te envió Supabase.';
    case 'EMAIL_ALREADY_REGISTERED':
      return 'Ese correo ya está registrado. Inicia sesión.';
    case 'WEAK_PASSWORD':
      return 'La contraseña es muy débil. Usa al menos 6 caracteres.';
    case 'VALIDATION_ERROR':
      return 'Revisa el correo y usa una contraseña de 6 a 72 caracteres.';
    case 'AUTH_RATE_LIMITED':
    case 'RATE_LIMITED':
      return 'Demasiados intentos. Espera un momento e inténtalo de nuevo.';
    case 'AUTH_NOT_CONFIGURED':
      return 'El inicio de sesión no está configurado en esta tienda.';
    case 'UNAUTHORIZED':
      return 'Tu sesión expiró. Vuelve a iniciar sesión.';
    case 'NETWORK_ERROR':
    case 'NETWORK_TIMEOUT':
      return 'No pudimos conectar con el servidor. Inténtalo de nuevo.';
    default:
      return 'No se pudo completar el inicio de sesión. Inténtalo de nuevo.';
  }
}

const billingStatuses: readonly BillingStatus[] = ['pending', 'issued', 'failed', 'not_requested'];

function readNullableString(value: Record<string, unknown>, key: string): string | null {
  const raw = value[key];
  return typeof raw === 'string' && raw.length > 0 ? raw : null;
}

export async function fetchInvoice(id: string, signal?: AbortSignal): Promise<InvoiceInfo> {
  const response = await request(`/api/v1/orders/${encodeURIComponent(id)}/invoice`, { signal });
  const value = response.payload;
  if (!isRecord(value) || !billingStatuses.includes(value.billing_status as BillingStatus)) {
    throw invalidResponse();
  }
  return {
    billing_status: value.billing_status as BillingStatus,
    invoice_id: readNullableString(value, 'invoice_id'),
    invoice_number: readNullableString(value, 'invoice_number')
  };
}

export function friendlyError(error: unknown, context: 'catalog' | 'create' | 'order'): FriendlyError {
  const code = error instanceof ApiError ? error.code : 'UNKNOWN';

  if (context === 'catalog') {
    if (code === 'NETWORK_TIMEOUT') {
      return {
        code,
        message: 'El catálogo tardó demasiado en responder. Revisa que la API esté disponible e inténtalo de nuevo.'
      };
    }
    if (code === 'NETWORK_ERROR') {
      return {
        code,
        message: 'No pudimos conectar con el catálogo. Revisa tu conexión e inténtalo de nuevo.'
      };
    }
    if (code === 'RESPONSE_INVALID') {
      return { code, message: 'El catálogo respondió con datos que no pudimos leer.' };
    }
    return { code, message: 'No pudimos cargar el catálogo. Inténtalo de nuevo.' };
  }

  if (context === 'create') {
    if (code === 'OUT_OF_STOCK') {
      return {
        code,
        message: 'La disponibilidad cambió y no hay stock suficiente. Actualizamos el catálogo; revisa tu carrito.'
      };
    }
    if (code === 'IDEMPOTENCY_CONFLICT') {
      return {
        code,
        message: 'El intento anterior no coincide con este carrito. Confirma de nuevo para crear un pedido nuevo.'
      };
    }
    if (code === 'VALIDATION_ERROR') {
      return {
        code,
        message: 'El pedido no pasó la validación del servidor. Revisa las cantidades y vuelve a intentarlo.'
      };
    }
    if (code === 'RATE_LIMITED') {
      return {
        code,
        message: 'Hay demasiadas solicitudes. Espera unos segundos y vuelve a intentarlo.'
      };
    }
    if (code === 'NETWORK_TIMEOUT') {
      return {
        code,
        message: 'La confirmación tardó demasiado. Tu carrito sigue intacto; reintenta y no se duplicará el pedido.'
      };
    }
    if (code === 'NETWORK_ERROR') {
      return {
        code,
        message: 'No pudimos conectar para confirmar. Tu carrito sigue intacto; reintenta y no se duplicará el pedido.'
      };
    }
    if (code === 'RESPONSE_INVALID') {
      return {
        code,
        message: 'No pudimos leer la confirmación. Reintenta para recuperar el mismo pedido.'
      };
    }
    return {
      code,
      message: 'El servidor no pudo crear el pedido. Tu carrito sigue intacto; inténtalo de nuevo.'
    };
  }

  if (code === 'NOT_FOUND') {
    return { code, message: 'No encontramos un pedido con ese ID.' };
  }
  if (code === 'NETWORK_TIMEOUT') {
    return { code, message: 'La consulta tardó demasiado. Inténtalo de nuevo.' };
  }
  if (code === 'NETWORK_ERROR') {
    return { code, message: 'No pudimos conectar para consultar el pedido. Inténtalo de nuevo.' };
  }
  if (code === 'RESPONSE_INVALID') {
    return { code, message: 'El servidor respondió con datos que no pudimos leer.' };
  }
  return { code, message: 'No pudimos consultar el pedido. Inténtalo de nuevo.' };
}
