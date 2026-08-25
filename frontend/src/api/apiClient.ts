/**
 * The app's single API client.
 *
 * Created once at module scope: the routing hook treats the client as an identity, and a
 * new one per render would re-run its effect. Same origin, so no base URL — in production
 * FastAPI serves this bundle itself, and in development Vite proxies `/api` to it.
 */
import { createApiClient } from './client'

export const apiClient = createApiClient()
