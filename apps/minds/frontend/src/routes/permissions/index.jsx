import { PermissionRequest } from '../../components/permissions/PermissionRequest.jsx';

// Generic permission-request shell with no backend-specific body. Used
// as a fallback / default landing in routes that have no specialised
// permission UI yet. The shell renders the rationale and approve/deny
// chrome; the body is left empty.
export function PermissionsIndexRoute(props) {
  return (
    <PermissionRequest
      agentId={props.agentId}
      requestId={props.requestId}
      wsName={props.wsName}
      rationale={props.rationale}
      displayName={props.displayName}
      accent={props.accent}
    />
  );
}
