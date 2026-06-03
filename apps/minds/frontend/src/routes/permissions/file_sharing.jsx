import { PermissionRequest } from '../../components/permissions/PermissionRequest.jsx';

// Latchkey file-sharing permission request dialog. Mirrors the
// predefined dialog's chrome but offers no per-permission choice: the
// request already names the (path, access) pair. A hidden checked
// ``permissions`` input enables the Approve button on first paint.
//
// Props:
//   agentId, requestId, wsName, rationale, accent  -- forwarded to
//                                                     PermissionRequest.
//   filePath          -- absolute host path the agent wants access to.
//   access            -- raw access mode (``READ`` or ``WRITE``); drives
//                       the badge color and the summary copy.
//   accessHumanLabel  -- lower-case rendering shown in the badge body.
export function PermissionsFileSharingRoute(props) {
  const isWrite = props.access === 'WRITE';
  const summary = isWrite ? (
    <>
      Approving will grant {props.wsName || props.agentId} and its sibling agents{' '}
      <strong>read and write</strong> access (including delete) to the following
      file/directory:
    </>
  ) : (
    <>
      Approving will grant {props.wsName || props.agentId} and its sibling agents{' '}
      <strong>read-only</strong> access to the following file:
    </>
  );
  const badgeClass = isWrite
    ? 'shrink-0 text-xs font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full bg-amber-50 text-amber-800 border border-amber-200'
    : 'shrink-0 text-xs font-semibold uppercase tracking-wide px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-800 border border-emerald-200';
  return (
    <PermissionRequest
      agentId={props.agentId}
      requestId={props.requestId}
      wsName={props.wsName}
      rationale={props.rationale}
      displayName={props.filePath}
      accent={props.accent}
      progressLabel="Granting permission..."
    >
      <p class="text-sm text-zinc-700 mb-3">{summary}</p>
      <div class="bg-white border border-zinc-200 rounded-xl p-4 flex items-center gap-3">
        <code class="text-sm font-mono text-zinc-900 break-all flex-1 min-w-0">
          {props.filePath}
        </code>
        <span class={badgeClass}>{props.accessHumanLabel}</span>
      </div>
      <input type="checkbox" name="permissions" value="file-sharing" checked hidden />
    </PermissionRequest>
  );
}
