import {describe,it,expect} from 'vitest';
import {ApiError} from './api';
import {isExplicitMutationRejection,mutationRequestOptions} from './useRecoverableMutation';
import {stageMutation} from './reviewRecovery';

describe('optional preview cancellation preserves durable mutation semantics',()=>{
  it('keeps the signal in transport options and out of the stored original body',()=>{
    const values=new Map<string,string>(),storage={getItem:(key:string)=>values.get(key)||null,setItem:(key:string,value:string)=>{values.set(key,value);}};
    const item=stageMutation(storage,'preview','/industry-mom-material-plans/preview',{review:{reviewer_id:'explicit'}},'same-key');
    const controller=new AbortController(),options=mutationRequestOptions(item.body,controller.signal);
    expect(options.signal).toBe(controller.signal);expect(JSON.parse(String(options.body))).toEqual(item.body);
    expect(values.get('preview')).not.toContain('signal');expect(options).not.toHaveProperty('readTimeoutMs');
    expect(mutationRequestOptions(item.body)).not.toHaveProperty('signal');
  });
  it('abort, network, unknown result and conflicts never mark the original request rejected',()=>{
    for(const error of [new DOMException('cancelled','AbortError'),new ApiError('network',0),new ApiError('contract',422,true),new ApiError('conflict',409),new ApiError('server',500)])
      expect(isExplicitMutationRejection(error)).toBe(false);
    expect(isExplicitMutationRejection(new ApiError('explicit client rejection',422))).toBe(true);
  });
});
