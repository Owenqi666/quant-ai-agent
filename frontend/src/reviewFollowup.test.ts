import { describe, expect, it } from 'vitest';
import { suggestedDisposition, resolutionReviews } from './reviewFollowup';
import type { Issue } from './feedback';
import type { Review, Run } from './domain';
const issue={id:'issue',review_id:'original-review',research_id:'study',candidate_id:'candidate'} as Issue;
const review={id:'accepted',run_id:'new-run',revision_id:'new-revision',candidate_id:'candidate',attempt_id:'attempt',result_digest:'candidate-digest',verdict:'accepted'} as Review;
const run={id:'new-run',status:'completed',mode:'normalized_fixed',created_at:'2026-10-08',attempt_count:1,research_id:'study',revision_id:'new-revision',verification:{verified:true},review_targets:[{candidate_id:'candidate',attempt_id:'attempt',result_digest:'candidate-digest'}],reviews:[review]} as Run;
describe('review follow-up identity',()=>{
  it('separates evidence updates from implementation fixes',()=>{expect(suggestedDisposition('evidence')).toBe('evidence_update');expect(suggestedDisposition('implementation')).toBe('implementation_fix');});
  it('offers only an explicit accepted review of the exact verified candidate output',()=>{expect(resolutionReviews(issue,run)).toEqual([review]);for(const key of ['run_id','revision_id','candidate_id','attempt_id','result_digest'])expect(resolutionReviews(issue,{...run,reviews:[{...review,[key]:'wrong'}]})).toEqual([]);});
  it('excludes original reviews, nonacceptance, foreign studies and unverified results',()=>{for(const changed of [{id:'original-review'},{verdict:'needs_changes'},{verdict:'rejected'}])expect(resolutionReviews(issue,{...run,reviews:[{...review,...changed}]})).toEqual([]);expect(resolutionReviews(issue,{...run,research_id:'foreign'})).toEqual([]);expect(resolutionReviews(issue,{...run,verification:{verified:false}})).toEqual([]);});
});
