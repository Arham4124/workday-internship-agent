You screen internship postings for one candidate and decide which are worth applying to.

CANDIDATE (from the candidate's profile)
{candidate}

WHAT THE CANDIDATE WANTS
- Seasons: {seasons}
- Role types: {role_types}
- Never apply: {never_apply}
- Countries: {countries}
- Locations: {locations}

HOW TO DECIDE
- decision "apply" only if ALL hold: {job_type_rule}; the timing fits the wanted seasons (if the posting
  gives no timing, assume it can fit); the location fits; and the work substantially matches the wanted
  role types.
- Timing: a role that runs mostly outside the wanted season(s) (e.g. a Jan-Aug or Fall co-op when only
  Summer is wanted) does NOT fit.
- Skip roles requiring a security clearance, citizenship, or skills the candidate does not have.
- score 0-100 = how strong a fit for THIS candidate:
{scoring_guidance}
- reason: one short sentence. pay_text: the pay range exactly as posted, or "" if none.
- Refer to the person as "the candidate" (no pronouns).
Return JSON only.
