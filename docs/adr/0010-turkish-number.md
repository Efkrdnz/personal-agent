# ADR 0010 — Pursue a +90 Turkish number, originating domestically

**Status:** accepted, 16 Sep 2026
**Decided by:** Big Efk

## Context

An earlier research pass concluded that Turkish numbers are closed to individuals. That was **wrong** — it
generalised from Verimor, which happens to be the one operator that genuinely demands a `vergi levhası`
("Bireysel abonelere hizmet verilmemektedir"). A dedicated sweep found Netgsm appears to accept individual
subscribers on T.C. Kimlik No alone, via e-Devlet with NFC identity verification, with self-service SIP
credentials and first-party AI-calling integration guides.

The structural finding that decides the approach: **outbound +90 caller ID only works if the call originates
domestically.** Every international-DID route fails independently — the cheap +90 DID class is sold
inbound-only, Telnyx and AVOXI require a verified Turkish company, and Turkey's BTKCLI terminates
foreign-originated calls presenting a Turkish CLI (Turkcell reported ~4M blocked in December 2025).

This matters only for one of the three call flows. You calling Jarvis, and Jarvis calling you, work fine on a
foreign number because you know to expect the call. Restaurants are the use case that needs +90 — and there
the failure is social, not technical: a Turkish restaurant seeing a +1 number at 19:00 on a Friday often will
not answer.

## Decision

Pursue a +90 number, **originating domestically**. Start procurement on day one of stage 0, in parallel with
everything else, because it blocks and nothing else does.

Primary path: Netgsm `bireysel` subscription. **One phone call settles it** — 0850 303 0 303, eight questions
written out in Turkish in [`../telephony.md`](../telephony.md). Question 3 is the one the project turns on:
can you register your own Asterisk and originate outbound with your own number presented? Netgsm's documented
"SIP Trunk" feature is an *inbound redirect*; outbound appears to require a registered extension.

Backup, needing no paperwork and testable the same evening: **your own SIM in an Asterisk `chan_mobile`
Bluetooth bridge.** The network asserts the caller ID from the SIM's own MSISDN, so nothing is spoofed and
nothing can be screened. HFP wideband is 16 kHz — a *better* match for Gemini Live than PSTN's 8 kHz.

## Architectural consequence

**LiveKit Cloud cannot be the SIP endpoint here.** Verified in code, not inferred: `livekit_sip.proto` has no
registrar, contact or expiry field of any kind; issue `livekit/sip#338` shows REGISTER answered `405 Method
Not Allowed`; PR `livekit/sip#774` adding it was still open as of 16 Sep 2026. Netgsm outbound appears to need
REGISTER.

Therefore Asterisk (`res_pjsip`) on a small Istanbul VPS terminates SIP in both directions and bridges media
over AudioSocket or ARI `externalMedia`. LiveKit, if used, is a pipe behind it — not an assistant.

## Caveat on confidence

Every Turkish operator and regulator domain (`netgsm.com.tr`, `verimor.com.tr`, `btk.gov.tr`,
`mevzuat.gov.tr`) is unreachable from any research sandbox — `curl` returns HTTP 000. All operator-side facts
are search-index-derived and marked unverified in `telephony.md`. **No further desk research can settle this.
The phone call can.**
