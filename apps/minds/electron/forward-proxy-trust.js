'use strict';

// How the shell trusts the local `mngr forward` proxy's TLS: by the proxy's
// own persistent local CA, read from the path the backend reports with the
// proxy's port, and by nothing else. Chromium does not know that CA, so its
// own verification of a proxy origin fails with "unknown authority" alone;
// the verdict here accepts exactly that case when the leaf really was signed
// by the CA, and leaves every other verdict (a name mismatch, an expired
// leaf, any origin that is not loopback) to Chromium.
//
// Kept free of any `electron` import so the verdict can be unit-tested under
// plain node; `main.js` installs it as the session's certificate verify proc.

const fs = require('fs');
const { X509Certificate } = require('crypto');

// Chromium's net error codes as the verify proc sees them.
const NET_OK = 0;
const NET_ERR_CERT_AUTHORITY_INVALID = -202;

// What the verify proc answers: trust the certificate, or let Chromium's own
// verdict stand (which, for a certificate it cannot chain, is to refuse it).
const VERDICT = Object.freeze({ TRUST: 0, DEFER_TO_CHROMIUM: -3 });

function isLoopbackHostname(hostname) {
  return hostname === 'localhost' || hostname.endsWith('.localhost') || hostname === '127.0.0.1';
}

function loadForwardProxyCa(caCertPath) {
  return new X509Certificate(fs.readFileSync(caCertPath));
}

// Whether `ca` issued the leaf: its name is the leaf's issuer, and its key
// verifies the leaf's signature. This is the proxy's whole claim, so the chain
// Chromium happened to assemble plays no part.
function isIssuedByCa(leafPem, ca) {
  let leaf;
  try {
    leaf = new X509Certificate(leafPem);
  } catch {
    return false;
  }
  return leaf.checkIssued(ca) && leaf.verify(ca.publicKey);
}

function decideCertificateVerdict({ hostname, errorCode, certificatePem }, ca) {
  if (!isLoopbackHostname(hostname)) return VERDICT.DEFER_TO_CHROMIUM;
  if (errorCode !== NET_OK && errorCode !== NET_ERR_CERT_AUTHORITY_INVALID) return VERDICT.DEFER_TO_CHROMIUM;
  return isIssuedByCa(certificatePem, ca) ? VERDICT.TRUST : VERDICT.DEFER_TO_CHROMIUM;
}

// Besides answering, the proc reports every loopback origin it leaves to
// Chromium: a workspace origin that fails TLS is otherwise only visible as a
// blank frame, and Chromium's own objection (`errorCode` and
// `verificationResult`) is the one fact needed to tell a leaf the proxy did
// not sign from a leaf Chromium rejects for another reason. Other origins are
// not reported; Chromium's verdict on them is not this proc's business.
function createCertificateVerifyProc(ca, log = console.warn) {
  return (request, callback) => {
    const verdict = decideCertificateVerdict(
      { hostname: request.hostname, errorCode: request.errorCode, certificatePem: request.certificate.data },
      ca,
    );
    if (verdict === VERDICT.DEFER_TO_CHROMIUM && isLoopbackHostname(request.hostname)) {
      log(
        `[forward-proxy-trust] Left the certificate for ${request.hostname} to Chromium ` +
          `(errorCode=${request.errorCode}, verificationResult=${request.verificationResult}, ` +
          `issuedByProxyCa=${isIssuedByCa(request.certificate.data, ca)})`,
      );
    }
    callback(verdict);
  };
}

module.exports = {
  NET_ERR_CERT_AUTHORITY_INVALID,
  NET_OK,
  VERDICT,
  createCertificateVerifyProc,
  decideCertificateVerdict,
  isLoopbackHostname,
  loadForwardProxyCa,
};
