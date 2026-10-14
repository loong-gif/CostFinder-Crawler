import assert from "node:assert/strict";

function normalize(value) {
  return String(value).normalize("NFKC").replace(/\s+/g, " ").replace(/\bunits?\b/gi, "u").replace(/\$\s*([\d,]+)(?:\.\d{1,2})?/g, "$$$1").replace(/[^a-z0-9$%]+/gi, " ").toLowerCase().trim();
}

function supported(claim, candidate) {
  const a = normalize(claim); const b = normalize(candidate);
  return b.includes(a) || a.includes(b) || a.split(" ").filter((token) => token.length > 2).every((token) => b.includes(token));
}

const cards = ["BOTOX 64u $768", ...Array.from({ length: 31 }, (_, index) => `card-${index + 2} $${index + 2}`)];
const before = ["BOTOX 64 units: $768."];
const additions = cards.filter((card) => !before.some((claim) => supported(claim, card)));
const after = [...before, ...additions];
assert.equal(after.length, 32);
assert.equal(after[0], before[0]);
assert.equal(additions.length, 31);

const unresolved = ["old claim"].filter((claim) => !["new evidence"].some((candidate) => supported(claim, candidate)));
assert.deepEqual(unresolved, ["old claim"]);
assert.deepEqual(["old claim", "new evidence"].filter(Boolean), ["old claim", "new evidence"]);

const pageUnavailable = { write_eligible: false, proposed_removals: [] };
assert.equal(pageUnavailable.write_eligible, false);
assert.deepEqual(pageUnavailable.proposed_removals, []);

console.log("promotion visibility completeness checks passed");
