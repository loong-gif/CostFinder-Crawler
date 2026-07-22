# Consumer Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing CostFinders consumer email/password registration and login flow production-usable with automatic profile creation, immediate sessions, and server-protected dashboards.

**Architecture:** Keep the existing Next.js 16 App Router, Header auth modal, Server Actions, Auth Context, and `@supabase/ssr` clients. Add the missing `public.profiles` contract with an `auth.users` trigger and owner-only RLS, simplify the consumer flow to email/password, and make Proxy preserve safe dashboard return paths.

**Tech Stack:** Next.js 16.2, React 19, TypeScript, Supabase Auth/Postgres/RLS, `@supabase/ssr`, Node `node:test` via the already-installed `tsx`, Python `pytest`.

## Global Constraints

- Scope is ordinary consumers only; do not modify business or admin registration behavior.
- Support email/password, logout, and password reset; remove consumer Magic Link and mandatory email/phone verification.
- Registration must return a usable session immediately; Supabase Confirm email must be disabled for this project.
- Do not add dependencies.
- Browser code may use only the Supabase publishable key; never expose a service-role or secret key.
- `verification_status='unverified'` must not block login and must not be represented as verified.
- Do not create claims, messages, notifications, or other consumer business tables in this change.
- Do not commit unless the user explicitly requests a commit.

---

## File Map

- Create `config/sql/m017_consumer_auth_profiles.sql`: canonical consumer profile table, trigger, grants, RLS policies, and migration assertions.
- Create `tests/test_consumer_auth_migration.py`: static contract test for security-critical migration clauses.
- Modify `frontend/src/lib/actions/auth.ts`: remove Magic Link and make successful sign-up require an immediate session.
- Modify `frontend/src/lib/context/authContext.tsx`: hydrate direct sign-up and expose profile initialization errors.
- Modify `frontend/src/components/features/signInForm.tsx`: retain password reset and remove Magic Link UI/state.
- Modify `frontend/src/components/features/signUpForm.tsx`: change success callback to session success rather than pending email.
- Modify `frontend/src/components/features/authModal.tsx`: reduce the modal to sign-up/sign-in and close only after user hydration.
- Create `frontend/src/lib/auth-redirect.ts`: validate dashboard return paths at the URL trust boundary.
- Create `frontend/src/lib/auth-redirect.test.ts`: runnable open-redirect regression check using `node:test`.
- Modify `frontend/src/proxy.ts`: preserve the intended dashboard path and explicitly configure the Proxy matcher.
- Modify `frontend/src/components/layout/globalHeader.tsx`: open sign-in from redirect parameters and return safely after authentication.
- Modify `frontend/package.json`: add the focused `test:auth` command using installed `tsx`.
- Modify `README.md` and `frontend/docs/ROUTES.md`: document the deployed auth flow, migration, Proxy, and configuration.

### Task 1: Add the consumer profile database contract

**Files:**
- Create: `config/sql/m017_consumer_auth_profiles.sql`
- Create: `tests/test_consumer_auth_migration.py`

**Interfaces:**
- Consumes: `auth.users(id, raw_user_meta_data)` and authenticated JWT `auth.uid()`.
- Produces: `public.profiles` matching `frontend/src/lib/actions/profile.ts::Profile`, plus `public.handle_new_consumer()` and trigger `on_auth_consumer_created`.

- [ ] **Step 1: Write the failing migration contract test**

Create `tests/test_consumer_auth_migration.py`:

```python
from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "config/sql/m017_consumer_auth_profiles.sql"
).read_text(encoding="utf-8").lower()


def test_consumer_profiles_migration_enforces_owner_access() -> None:
    required = (
        "references auth.users(id) on delete cascade",
        "enable row level security",
        "to authenticated",
        "(select auth.uid()) = id",
        "with check",
        "security definer",
        "set search_path = ''",
        "raw_user_meta_data ->> 'role'",
        "revoke all on function public.handle_new_consumer() from public",
        "after insert on auth.users",
    )
    assert all(clause in SQL for clause in required)
```

- [ ] **Step 2: Run the test and confirm the intended failure**

Run:

```bash
pytest tests/test_consumer_auth_migration.py -q
```

Expected: FAIL because `config/sql/m017_consumer_auth_profiles.sql` does not exist.

- [ ] **Step 3: Add the idempotent migration**

Create `config/sql/m017_consumer_auth_profiles.sql` with:

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS public.profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  first_name text,
  last_name text,
  avatar_url text,
  phone text,
  phone_verified_at timestamptz,
  verification_status text NOT NULL DEFAULT 'unverified'
    CHECK (verification_status IN ('unverified', 'email_verified', 'phone_verified', 'fully_verified')),
  location_city text,
  location_state text,
  alerts_email boolean NOT NULL DEFAULT true,
  alerts_sms boolean NOT NULL DEFAULT false,
  favorite_categories text[] NOT NULL DEFAULT '{}'::text[],
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
GRANT SELECT, UPDATE ON public.profiles TO authenticated;

DROP POLICY IF EXISTS "consumer_select_own_profile" ON public.profiles;
CREATE POLICY "consumer_select_own_profile"
  ON public.profiles FOR SELECT TO authenticated
  USING ((SELECT auth.uid()) = id);

DROP POLICY IF EXISTS "consumer_update_own_profile" ON public.profiles;
CREATE POLICY "consumer_update_own_profile"
  ON public.profiles FOR UPDATE TO authenticated
  USING ((SELECT auth.uid()) = id)
  WITH CHECK ((SELECT auth.uid()) = id);

CREATE OR REPLACE FUNCTION public.handle_new_consumer()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  IF NEW.raw_user_meta_data ->> 'role' IN ('business', 'admin') THEN
    RETURN NEW;
  END IF;

  INSERT INTO public.profiles (id, first_name, last_name)
  VALUES (
    NEW.id,
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'first_name'), ''),
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'last_name'), '')
  )
  ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.handle_new_consumer() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.handle_new_consumer() FROM anon, authenticated;

DROP TRIGGER IF EXISTS on_auth_consumer_created ON auth.users;
CREATE TRIGGER on_auth_consumer_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_consumer();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid = 'public.profiles'::regclass AND relrowsecurity
  ) THEN
    RAISE EXCEPTION 'profiles RLS must be enabled';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'on_auth_consumer_created' AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'consumer profile trigger is missing';
  END IF;
END;
$$;

COMMIT;
```

- [ ] **Step 4: Run local contract and dry-run checks**

Run:

```bash
pytest tests/test_consumer_auth_migration.py -q
python scripts/apply_sql_migration.py config/sql/m017_consumer_auth_profiles.sql --dry-run
```

Expected: pytest PASS; dry-run prints SQL and performs no write.

- [ ] **Step 5: Apply only after the native write confirmation**

Use the Supabase migration tool or the repository migration runner after showing the SQL payload. Do not apply if `profiles`, `handle_new_consumer`, or `on_auth_consumer_created` exists with a conflicting definition.

Expected postconditions: `profiles.rls_enabled=true`; ownership policies and trigger are present.

### Task 2: Make registration produce a hydrated consumer session

**Files:**
- Modify: `frontend/src/lib/actions/auth.ts`
- Modify: `frontend/src/lib/context/authContext.tsx`
- Modify: `frontend/src/components/features/signUpForm.tsx`
- Modify: `frontend/src/components/features/signInForm.tsx`
- Modify: `frontend/src/components/features/authModal.tsx`

**Interfaces:**
- Consumes: `signUpAction(email, password, firstName?, lastName?)`.
- Produces: successful sign-up only when `data.session` exists and the profile trigger has completed; `AuthModal.onSuccess?: () => void`.

- [ ] **Step 1: Remove unsupported authentication choices**

Delete `sendMagicLinkAction` from `frontend/src/lib/actions/auth.ts`. In `signInForm.tsx`, remove its import, Magic Link state, handler, divider, button, and status messages. Keep `resetPasswordAction` and its enumeration-safe response.

- [ ] **Step 2: Tighten the sign-up result**

After `supabase.auth.signUp`, require an immediate session:

```ts
if (!data.session) {
  return {
    success: false,
    error: 'Account created without a session. Disable Confirm email in Supabase Auth settings.',
  }
}

const { error: profileError } = await supabase
  .from('profiles')
  .select('id')
  .eq('id', data.user.id)
  .single()

if (profileError) {
  return {
    success: false,
    error: 'Your account could not be initialized. Please try again.',
  }
}

return { success: true }
```

Remove `needsEmailVerification` from `AuthResult` and all branches that consume it.

- [ ] **Step 3: Surface profile initialization failures**

In `hydrateUser()`, replace the silent missing-profile state with:

```ts
if (!profileResult.success || !profileResult.profile) {
  setState({
    user: null,
    isAuthenticated: false,
    isLoading: false,
    error: 'Your account profile could not be loaded. Please sign out and try again.',
  })
  setSavedDeals([])
  return
}
```

Keep `getSession()` only as the client-side fast path; continue using `getUser()` before trusting identity.

- [ ] **Step 4: Simplify modal completion**

Change `SignUpFormProps.onSuccess` to `() => void`, call `onSuccess?.()` with no email, and reduce `AuthView` to:

```ts
type AuthView = 'signUp' | 'signIn'
```

Remove `EmailVerification`, `PhoneVerification`, `pendingEmail`, and verification-status routing. Both sign-up and sign-in should set `awaitingSignIn=true`; when hydration finishes with `state.user`, invoke `onSuccess` and close the modal. If hydration finishes with `state.error`, keep the modal open.

- [ ] **Step 5: Run static verification**

Run:

```bash
cd frontend
npm run lint
npm run build
```

Expected: both commands exit 0; no references to `sendMagicLinkAction` remain.

### Task 3: Preserve safe dashboard return paths

**Files:**
- Create: `frontend/src/lib/auth-redirect.ts`
- Create: `frontend/src/lib/auth-redirect.test.ts`
- Modify: `frontend/src/proxy.ts`
- Modify: `frontend/src/components/layout/globalHeader.tsx`
- Modify: `frontend/package.json`

**Interfaces:**
- Produces: `safeDashboardPath(value: string | null): string | null`.
- Consumes: `signin=required&next=/dashboard...` query parameters emitted by Proxy.

- [ ] **Step 1: Write the failing redirect security test**

Create `frontend/src/lib/auth-redirect.test.ts`:

```ts
import assert from 'node:assert/strict'
import test from 'node:test'
import { safeDashboardPath } from './auth-redirect'

test('accepts only local consumer dashboard paths', () => {
  assert.equal(safeDashboardPath('/dashboard'), '/dashboard')
  assert.equal(safeDashboardPath('/dashboard/settings'), '/dashboard/settings')
  assert.equal(safeDashboardPath('//evil.example'), null)
  assert.equal(safeDashboardPath('https://evil.example'), null)
  assert.equal(safeDashboardPath('/admin/dashboard'), null)
  assert.equal(safeDashboardPath('/business/dashboard'), null)
})
```

Add to `frontend/package.json`:

```json
"test:auth": "tsx --test src/lib/auth-redirect.test.ts"
```

Run `npm run test:auth`.

Expected: FAIL because `auth-redirect.ts` is missing.

- [ ] **Step 2: Add the minimum return-path validator**

Create `frontend/src/lib/auth-redirect.ts`:

```ts
export function safeDashboardPath(value: string | null): string | null {
  if (value === '/dashboard' || value?.startsWith('/dashboard/')) return value
  return null
}
```

Run `npm run test:auth`.

Expected: PASS.

- [ ] **Step 3: Preserve the requested path in Proxy**

For unauthenticated consumer dashboard requests, set:

```ts
url.pathname = '/'
url.searchParams.set('signin', 'required')
url.searchParams.set('next', pathname)
```

Export a matcher that excludes static assets while allowing session refresh and dashboard protection:

```ts
export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
}
```

Do not broaden or redesign business/admin authorization in this consumer-only change.

- [ ] **Step 4: Consume redirect parameters in the Header**

Use `useSearchParams`, `useRouter`, and `safeDashboardPath`. When `signin=required`, open the sign-in modal. On auth success, close the modal and `router.replace(safeDashboardPath(next) ?? pathname)`; remove the auth query parameters from history.

Do not accept absolute URLs, protocol-relative URLs, business routes, or admin routes.

- [ ] **Step 5: Verify the Proxy and redirect helper**

Run:

```bash
cd frontend
npm run test:auth
npm run lint
npm run build
```

Expected: all exit 0 and the production build reports Proxy/middleware output rather than an empty registration.

### Task 4: Verify live authentication and update documentation

**Files:**
- Modify: `README.md`
- Modify: `frontend/docs/ROUTES.md`

**Interfaces:**
- Consumes: deployed `m017` schema and Supabase Auth Confirm email setting.
- Produces: reproducible setup and verification instructions.

- [ ] **Step 1: Confirm the required Supabase Auth setting**

In Supabase Dashboard, disable **Authentication → Providers → Email → Confirm email**. MCP does not expose this Auth configuration; do not use browser automation unless the user explicitly requests it.

- [ ] **Step 2: Run database postflight checks**

Use read-only Supabase inspection to verify:

- `public.profiles` exists and RLS is enabled.
- `id` references `auth.users(id)` with `ON DELETE CASCADE`.
- owner-only SELECT and UPDATE policies exist.
- `on_auth_consumer_created` invokes `public.handle_new_consumer()`.

- [ ] **Step 3: Run the end-to-end acceptance path**

With a disposable unique email:

1. Register with first and last name.
2. Confirm the modal closes without verification screens.
3. Confirm Header displays Dashboard.
4. Confirm the profile row contains the same Auth user ID and names.
5. Open `/dashboard/settings`, refresh, and confirm the session survives.
6. Sign out and request `/dashboard/settings`; confirm redirect to `/?signin=required&next=/dashboard/settings`.
7. Sign in and confirm return to `/dashboard/settings`.
8. Request password reset and confirm the enumeration-safe success message.
9. Delete the disposable Auth user and confirm profile cascade deletion.

- [ ] **Step 4: Update project documentation**

Add to `README.md`:

```text
Consumer auth: Supabase email/password via the frontend Header modal.
Schema prerequisite: config/sql/m017_consumer_auth_profiles.sql.
Supabase Auth prerequisite: Confirm email disabled for immediate sessions.
```

Update `frontend/docs/ROUTES.md` so Access Control states that `src/proxy.ts` performs server-side session refresh and route protection, with `AuthenticatedDashboardLayout` retained only as a client fallback.

- [ ] **Step 5: Run the final verification suite**

Run:

```bash
pytest tests/test_consumer_auth_migration.py -q
cd frontend
npm run test:auth
npm run lint
npm run build
```

Expected: every command exits 0. Re-run read-only Supabase table/policy/trigger inspection and confirm the disposable test user has been removed.
