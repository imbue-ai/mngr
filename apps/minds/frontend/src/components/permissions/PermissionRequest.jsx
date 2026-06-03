import { Show, createMemo, createSignal } from 'solid-js';
import { Card } from '../ui/Card.jsx';
import { Notice } from '../ui/Notice.jsx';
import { closeModal } from '../../lib/electron_bridge.js';

// Generic shell around a permission-request dialog. Mirrors the Jinja
// templates/permissions.html base: full-screen backdrop, dialog card,
// header / rationale / body / actions / progress / error blocks. Backend-
// specific dialogs (the predefined and file-sharing permission flows)
// supply the body via children, and customise visible state via props.
//
// Form-submission contract:
//   * On submit, POSTs a FormData payload to /requests/<id>/grant.
//   * Approve is enabled as soon as any ``permissions`` checkbox in the
//     body is checked; the parent can hook in extra logic via
//     ``onPermissionsChange``.
//   * The grant response is parsed as JSON. ``outcome === "GRANTED"``
//     closes the modal; ``NEEDS_MANUAL_CREDENTIALS`` shows the manual
//     setup notice; anything else shows the generic error.
export function PermissionRequest(props) {
  const [errorMessage, setErrorMessage] = createSignal('');
  const [manualMessage, setManualMessage] = createSignal('');
  const [manualCommand, setManualCommand] = createSignal('');
  const [showProgress, setShowProgress] = createSignal(false);
  const [submitting, setSubmitting] = createSignal(false);
  const [approveDisabled, setApproveDisabled] = createSignal(true);

  const wsLabel = createMemo(() => props.wsName || props.agentId);
  const accentStyle = createMemo(() =>
    props.accent ? `--workspace-accent: ${props.accent};` : undefined,
  );

  const dismiss = () => {
    if (!closeModal()) {
      if (typeof window !== 'undefined') window.location.href = '/';
    }
  };

  const onBackdropClick = (event) => {
    if (event.target === event.currentTarget) dismiss();
  };

  const refreshApproveState = (form) => {
    const checked = form.querySelector('input[name="permissions"]:checked') !== null;
    setApproveDisabled(!checked);
  };

  const onFormInput = (event) => {
    refreshApproveState(event.currentTarget);
    props.onPermissionsChange?.(event);
  };

  const onMountForm = (form) => {
    refreshApproveState(form);
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    setApproveDisabled(true);
    setSubmitting(true);
    setErrorMessage('');
    setManualMessage('');
    setManualCommand('');
    setShowProgress(true);
    const data = new FormData(form);
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: data,
        credentials: 'same-origin',
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }
      const payload = await response.json();
      if (payload.outcome === 'GRANTED') {
        dismiss();
        return;
      }
      setShowProgress(false);
      if (payload.outcome === 'NEEDS_MANUAL_CREDENTIALS') {
        setManualCommand(payload.set_credentials_example || '');
        setManualMessage(payload.message || '');
        refreshApproveState(form);
        setSubmitting(false);
        return;
      }
      setErrorMessage(payload.message || 'Authorization failed.');
      refreshApproveState(form);
      setSubmitting(false);
    } catch (err) {
      setShowProgress(false);
      setErrorMessage(err && err.message ? err.message : String(err));
      refreshApproveState(form);
      setSubmitting(false);
    }
  };

  const handleDeny = () => {
    setApproveDisabled(true);
    // Fire-and-forget; matches the legacy behavior so the agent gets
    // notified even after the dialog navigates away.
    if (typeof fetch === 'function') {
      fetch(`/requests/${props.requestId}/deny`, {
        method: 'POST',
        credentials: 'same-origin',
        keepalive: true,
      }).catch(() => {});
    }
    dismiss();
  };

  const onKeyDown = (event) => {
    if (event.key === 'Escape') dismiss();
  };

  return (
    <div
      class="bg-transparent text-zinc-900 font-sans antialiased min-h-screen"
      attr:style={accentStyle()}
      onKeyDown={onKeyDown}
    >
      <div
        id="permissions-backdrop"
        class="fixed inset-0 bg-black/40 flex items-center justify-center p-6"
        onClick={onBackdropClick}
      >
        <div
          id="permissions-dialog"
          class="relative w-full max-w-[640px] max-h-full flex flex-col bg-white border border-zinc-200 rounded-2xl shadow-xl overflow-hidden"
        >
          <button
            type="button"
            id="permissions-close-btn"
            aria-label="Close"
            onClick={dismiss}
            class="absolute top-3 right-3 z-10 inline-flex items-center justify-center w-8 h-8 rounded-md text-zinc-400 hover:text-zinc-700 hover:bg-zinc-100 transition-colors cursor-pointer"
          >
            <svg
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
              class="w-5 h-5"
            >
              <path
                fill-rule="evenodd"
                clip-rule="evenodd"
                d="M4.22 4.22a.75.75 0 0 1 1.06 0L10 8.94l4.72-4.72a.75.75 0 1 1 1.06 1.06L11.06 10l4.72 4.72a.75.75 0 1 1-1.06 1.06L10 11.06l-4.72 4.72a.75.75 0 0 1-1.06-1.06L8.94 10 4.22 5.28a.75.75 0 0 1 0-1.06Z"
              />
            </svg>
          </button>
          <div class="overflow-y-auto min-h-0 p-6 sm:p-7">
            <h1 class="text-2xl font-semibold text-zinc-900 leading-tight pr-8">
              Permission request: {props.displayName || ''}
            </h1>
            <Card extra="mt-6">
              <div>
                <p class="text-base font-semibold text-zinc-900 mb-1">
                  {wsLabel()} says:
                </p>
                <p class="text-sm italic whitespace-pre-wrap">{props.rationale || ''}</p>
              </div>
            </Card>
            <form
              id="permissions-form"
              method="POST"
              action={`/requests/${props.requestId}/grant`}
              class="mt-6"
              onSubmit={handleSubmit}
              onInput={onFormInput}
              onChange={onFormInput}
              ref={(el) => el && onMountForm(el)}
            >
              {props.children}
              <div class="flex gap-2 mt-5 justify-end">
                <button
                  type="button"
                  onClick={handleDeny}
                  disabled={submitting()}
                  class="inline-flex items-center justify-center gap-1.5 px-3.5 py-2 rounded-md font-medium text-sm leading-tight transition-colors cursor-pointer no-underline whitespace-nowrap bg-red-50 text-red-600 border border-red-200 hover:bg-red-100"
                >
                  Deny
                </button>
                <button
                  type="submit"
                  id="permissions-approve-btn"
                  disabled={approveDisabled() || submitting()}
                  class="inline-flex items-center justify-center gap-1.5 px-3.5 py-2 rounded-md font-medium text-sm leading-tight transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer no-underline whitespace-nowrap bg-emerald-800 text-emerald-50 border border-transparent hover:bg-emerald-900"
                >
                  Approve
                </button>
              </div>
            </form>
            <Show when={showProgress()}>
              <div id="permissions-progress" class="mt-4">
                <Notice variant="info">
                  <span class="font-medium">
                    {props.progressLabel || 'Granting permission...'}
                  </span>
                  <Show when={props.progressDetail}>
                    {' '}
                    {props.progressDetail}
                  </Show>
                </Notice>
              </div>
            </Show>
            <Show when={manualMessage() || manualCommand()}>
              <div id="permissions-manual-credentials" class="mt-4">
                <Notice variant="info">
                  <p class="font-medium">Manual credential setup required</p>
                  <p class="mt-1">{manualMessage()}</p>
                  <p class="mt-2 text-sm">
                    Obtain credentials from the provider, replace any{' '}
                    <code>&lt;placeholders&gt;</code> below with them, run the command in
                    a terminal, then click Approve again:
                  </p>
                  <pre class="mt-2 bg-zinc-900 text-zinc-100 rounded-md p-3 text-xs overflow-x-auto">
                    <code>{manualCommand()}</code>
                  </pre>
                </Notice>
              </div>
            </Show>
            <Show when={errorMessage()}>
              <div id="permissions-error" class="mt-4">
                <Notice variant="error">
                  <p class="font-medium">Authorization failed</p>
                  <p>{errorMessage()}</p>
                </Notice>
              </div>
            </Show>
          </div>
        </div>
      </div>
    </div>
  );
}
