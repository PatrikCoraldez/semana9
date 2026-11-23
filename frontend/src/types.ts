export interface Product {
  id: string;
  name: string;
  description: string;
  category: string;
  price_cents: number;
  original_price_cents: number;
  stock: number;
  image: string;
}

export interface Campaign {
  name: string;
  ends_at: string;
  currency: string;
}

export interface Catalog {
  items: Product[];
  campaign: Campaign;
}

export type NotificationStatus = 'pending' | 'sent' | 'failed';

/** S11: estado local de facturación (independiente de la notificación). */
export type BillingStatus = 'pending' | 'issued' | 'failed' | 'not_requested';

/** Sesión de cliente (Supabase Auth vía el backend S09). */
export interface AuthSession {
  access_token: string;
  refresh_token: string;
  expires_at: number; // segundos Unix
  user: { id: string; email: string };
}

export interface MyOrderSummary {
  id: string;
  created_at: string;
  total_cents: number;
  units: number;
  notification_status: NotificationStatus;
  billing_status: BillingStatus;
  invoice_number: string | null;
}

export interface InvoiceDetail {
  number: string;
  currency: string;
  created_at: string;
  items: OrderLine[];
  total_cents: number;
}

export interface InvoiceInfo {
  billing_status: BillingStatus;
  invoice_id: string | null;
  invoice_number: string | null;
  invoice: InvoiceDetail | null;
}

export interface OrderLine {
  product_id: string;
  name: string;
  quantity: number;
  unit_price_cents: number;
}

export interface Order {
  id: string;
  status: 'confirmed';
  notification_status: NotificationStatus;
  items: OrderLine[];
  total_cents: number;
  created_at: string;
}

export interface CartLine {
  productId: string;
  quantity: number;
}

export interface OrderRequestBody {
  items: Array<{
    product_id: string;
    quantity: number;
  }>;
}
