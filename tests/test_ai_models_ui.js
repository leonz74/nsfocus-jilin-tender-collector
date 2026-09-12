"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function harness(fetch) {
  const source = fs.readFileSync(path.resolve(__dirname, '../src/tender_downloader/webui/static/app.js'), 'utf8');
  const context = vm.createContext({fetch, Headers, URL, document: {addEventListener() {}, querySelector: () => ({content: 'csrf'})}});
  vm.runInContext(source.replace(/\n\}\)\(\);\n$/, `
    const field = (value='') => ({value, checked: true, classList: {remove() {}}, setCustomValidity() {},
      focus() {}, type: 'password', options: [], replaceChildren(...rows) {this.options=rows;}, append(row) {this.options.push(row);}});
    for (const n of ['aiProviderPreset', 'aiProtocol', 'aiEndpoint', 'aiModel', 'aiKeyEnv', 'apiKey',
      'aiModelPreset', 'aiModelsStatus', 'aiKeyStatus', 'aiDeleteKey', 'aiSaveKey', 'aiFetchModels',
      'aiConnectionStatus', 'aiEnabled']) elements[n]=field();
    elements.aiProviderPreset.value='custom'; elements.aiProtocol.value='openai_compatible';
    elements.aiEndpoint.value='https://model.example/v1/chat/completions'; elements.aiKeyEnv.value='TEST_KEY';
    elements.apiKey.value='key-a';
    createOption = (value,label) => ({value,label}); setButtonsBusy = () => {};
    const toasts=[]; showToast=(...args)=>toasts.push(args);
    collectAiTestConfiguration = () => ({ai: {enabled:true, protocol:elements.aiProtocol.value,
      endpoint:elements.aiEndpoint.value, model:elements.aiModel.value, api_key_env:elements.aiKeyEnv.value},http:{}});
    validateAiTestConfiguration = (_a,_h,key) => Boolean(key || hasRememberedAiKey());
    globalThis.h={elements,toasts,fetchAiModels,saveAiKey,deleteAiKey,refreshAiKeyStatus,
      renderAiModelOptions,handleAiModelPresetChange,invalidateAiModels,hasRememberedAiKey,
      currentCatalog:()=>aiModelCatalog};
  })();\n`), context);
  return context.h;
}
const response = body => ({ok:true,text:async()=>JSON.stringify(body)});

test('fresh endpoint models appear even when absent from all presets, and can be selected', async () => {
  const h=harness(async()=>response({ok:true,complete:true,pages:2,models:[{id:'future-new',label:'Future'},{id:'fine-tune',label:'Fine tune'}]}));
  await h.fetchAiModels();
  assert.deepEqual(Array.from(h.elements.aiModelPreset.options,m=>m.value),['custom','future-new','fine-tune']);
  h.elements.aiModelPreset.value='future-new'; h.handleAiModelPresetChange();
  assert.equal(h.elements.aiModel.value,'future-new');
  h.renderAiModelOptions(null,'future-new');
  assert.equal(h.elements.aiModelPreset.value,'future-new','config redraw keeps fetched selection');
});

test('a late response for an old key cannot replace the current models', async () => {
  let resolve;
  const h=harness(()=>new Promise(r=>resolve=r));
  const request=h.fetchAiModels();
  h.elements.apiKey.value='key-b'; h.invalidateAiModels();
  resolve(response({ok:true,complete:true,models:[{id:'wrong-key-model',label:'Wrong'}]}));
  await request;
  assert.equal(h.currentCatalog(),null);
  assert.equal(h.elements.aiModelPreset.options.length,1);
});

test('save clears the input, reports persistence, then fetches models using saved credential', async () => {
  const requests=[];
  const h=harness(async(url,options)=>{
    const body=JSON.parse(options.body); requests.push({url,body});
    if(url.endsWith('/save')) return response({ok:true,saved:true,supported:true});
    return response({ok:true,complete:true,models:[{id:'saved-key-model',label:'Saved'}]});
  });
  await h.saveAiKey();
  assert.equal(h.elements.apiKey.value,'');
  assert.equal(requests[0].body.api_key,'key-a');
  assert.equal(requests[1].url,'/api/ai-models');
  assert.equal(requests[1].body.api_key,'');
  assert.equal(h.hasRememberedAiKey(),true);
  assert.match(h.elements.aiKeyStatus.textContent,/已保存到 Mac 钥匙串/);
});

test('after reload saved key state enables empty-input model fetching; other endpoints do not inherit it', async () => {
  const h=harness(async()=>response({ok:true,saved:true,supported:true}));
  h.elements.apiKey.value='';
  await h.refreshAiKeyStatus();
  assert.equal(h.hasRememberedAiKey(),true);
  h.elements.aiEndpoint.value='https://other.example/v1/chat/completions';
  assert.equal(h.hasRememberedAiKey(),false);
});

test('failed refresh removes old model choices and partial responses are visibly incomplete', async () => {
  let call=0;
  const h=harness(async()=>{
    if(call++) throw new Error('HTTP 401');
    return response({ok:true,complete:false,pages:1,models:[{id:'partial',label:'Partial'}],message:'next page failed'});
  });
  await h.fetchAiModels();
  assert.match(h.elements.aiModelsStatus.textContent,/列表不完整/);
  await h.fetchAiModels();
  assert.equal(h.elements.aiModelPreset.options.length,1);
  assert.match(h.elements.aiModelsStatus.textContent,/获取失败/);
});

test('failed save keeps the entered key and never claims it is saved', async () => {
  const h=harness(async()=>{throw new Error('Keychain locked');});
  await h.saveAiKey();
  assert.equal(h.elements.apiKey.value,'key-a');
  assert.match(h.elements.aiKeyStatus.textContent,/保存失败/);
  assert.ok(!h.hasRememberedAiKey());
});
