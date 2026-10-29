function sameJson(left, right) {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

export function compareCandidateSnapshot(candidate, current) {
  const expected = {
    plan_id: candidate.plan_id,
    business_id: candidate.business_id,
    membership_name: candidate.membership_name,
    source_url: candidate.source_url,
    updated_at: candidate.updated_at,
    benefits: candidate.current_benefits,
  };
  const actual = {
    plan_id: current.plan_id,
    business_id: current.business_id,
    membership_name: current.membership_name,
    source_url: current.source_url,
    updated_at: current.updated_at,
    benefits: current.benefits,
  };
  if (sameJson(current.benefits, candidate.suggested_benefits)) return { status: "already_applied", expected, actual };
  const conflicts = Object.keys(expected).filter(key => key === "benefits" ? !sameJson(actual[key], expected[key]) : actual[key] !== expected[key]);
  return conflicts.length ? { status: "conflict", conflicts, expected, actual } : { status: "ready", expected, actual };
}
