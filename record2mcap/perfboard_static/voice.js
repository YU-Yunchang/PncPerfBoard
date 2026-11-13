// Voice ingestion and evidence playback share the existing Run/event views.
let voicePoll=null,voiceRequest=0,voiceReturnFocus=null,voiceAvailable=true;
const voiceTime=value=>new Date(value).toLocaleString("en-GB",{timeZone:"Asia/Shanghai",hour12:false})+" CST";
const voiceDuration=ms=>{
  const seconds=Math.max(0,Math.round((Number(ms)||0)/1000));
  return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,"0")}`;
};
const voiceAudioIcon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M9 18V5l11-2v13M9 9l11-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="17" cy="16" rx="3" ry="3"/></svg>';
function voiceNotice(message,tone="info"){
  const notice=$("voice-status");if(!notice)return;
  notice.textContent=message;notice.dataset.tone=tone;notice.hidden=!message;
}
function recordingCard(r){
  const busy=["queued","processing"].includes(r.status),locked=r.event_count>0;
  const labels={uploaded:"Uploaded",queued:"Queued",processing:"Transcribing",ready:locked?"Events imported":"Transcript ready",failed:"Failed",interrupted:"Interrupted"};
  const start=r.start_time_ns?new Date(Number(r.start_time_ns)/1e6).toISOString():"";
  const timeSource=r.time_source==="manual"?"Manual start time":r.raw_start_time?"From recording metadata":"Start time required";
  const transcribe=`<button data-transcribe="${r.id}" ${busy||locked||!voiceAvailable?"disabled":""} class="${r.status==="ready"?"secondary":""}">${r.status==="ready"?"Transcribe again":["failed","interrupted"].includes(r.status)?"Retry transcription":"Transcribe"}</button>`;
  return `<article class="recording-card" data-recording="${r.id}" aria-label="${esc(r.filename)}">
    <header class="recording-header"><div class="recording-identity"><span class="recording-icon">${voiceAudioIcon}</span><div><h3 class="recording-title" title="${esc(r.filename)}">${esc(r.filename)}</h3><div class="recording-meta"><span>${voiceDuration(r.duration_ms)} duration</span>${locked?`<span>${r.event_count} imported events</span>`:""}</div></div></div><span class="recording-status" data-state="${esc(r.status)}">${esc(labels[r.status]||r.status)}</span></header>
    <div class="recording-body"><div class="recording-time"><span class="recording-label">Recording start <span>· ${timeSource}</span></span><div class="recording-time-value">${start?esc(voiceTime(start)):"Confirm the start time before transcribing"}</div>
      <details class="recording-time-editor" ${!start?"open":""}><summary>${locked?"View start time":"Adjust start time"}</summary><div class="recording-time-fields"><label>Start time with timezone<input data-start="${r.id}" value="${esc(start)}" placeholder="2026-09-27T13:29:11+08:00" ${busy||locked?"disabled":""}></label><small class="muted">${locked?"Start time is locked after event import.":busy?"Start time cannot be changed during transcription.":"Use ISO 8601 with a timezone, e.g. +08:00. Displayed above in Beijing time."}</small>${r.status==="ready"&&!locked?transcribe:""}</div></details>
      ${locked?'<span class="recording-lock">Transcript locked after event import</span>':""}</div>
      <div class="recording-actions">${r.status==="ready"?`<a class="voice-button secondary" href="/api/recordings/${r.id}/transcript">Download transcript JSON</a><input type="file" accept=".json" data-events="${r.id}" hidden><button data-choose-events="${r.id}">Import event JSON</button>`:busy?"":transcribe}</div></div>
    ${busy?`<div class="recording-progress"><div class="recording-progress-copy"><span data-progress-stage>${esc(r.stage||labels[r.status])}</span><span data-progress-time>${voiceDuration(r.processed_ms)} / ${voiceDuration(r.duration_ms)}</span></div><progress class="recording-progress-bar ${!r.duration_ms||r.stage!=="Transcribing"?"recording-progress-indeterminate":""}" aria-label="Transcription progress" max="${Math.max(1,r.duration_ms||0)}" ${r.duration_ms&&r.stage==="Transcribing"?`value="${Math.min(r.duration_ms,r.processed_ms||0)}"`:""}></progress></div>`:""}
    ${r.error?`<div class="recording-error" role="alert">${esc(r.error)}</div>`:""}
  </article>`;
}
document.addEventListener("DOMContentLoaded",()=>{
  const card=document.createElement("div");card.className="card";card.id="voice-panel";
  document.querySelector(".files").before(card);
  const modal=document.createElement("div");modal.id="voice-modal";modal.className="modal";modal.hidden=true;
  modal.innerHTML='<div class="modal-card voice-evidence-card" role="dialog" aria-modal="true" aria-labelledby="voice-title"><div class="head"><h2 id="voice-title">Voice Evidence</h2><button id="voice-close" class="secondary">Close</button></div><div id="voice-evidence"></div></div>';
  document.body.append(modal);
  $("voice-close").onclick=()=>{modal.querySelectorAll("audio").forEach(audio=>audio.pause());modal.hidden=true;if(voiceReturnFocus?.isConnected)voiceReturnFocus.focus({preventScroll:true})};
  modal.addEventListener("keydown",e=>{
    if(e.key==="Escape"){e.preventDefault();$("voice-close").click()}
    if(e.key!=="Tab")return;
    const controls=[...modal.querySelectorAll("button:not(:disabled),select:not(:disabled),summary,audio[controls]")].filter(el=>el.getClientRects().length);
    const first=controls[0],last=controls.at(-1);
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus()}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus()}
  });
  $("series").addEventListener("dblclick",e=>{const marker=e.target.closest("[data-event-id]");if(!marker)return;const event=state.timeline.events.find(v=>v.id===Number(marker.dataset.eventId));if(event?.voice||event?.voice_reports?.length){e.stopImmediatePropagation();openVoiceEvidence(event)}},true);
});

function renderVoicePanel(){
  const panel=$("voice-panel");if(!panel||!state.run)return;
  const id=state.run.id;
  if(Number(panel.dataset.run)!==id){
    panel.dataset.run=id;
    panel.innerHTML=`<div class="voice-panel-header"><div><h2>Road-test Recordings</h2><p class="voice-panel-description">Turn road-test audio into events with traceable voice evidence.</p></div><ol class="voice-workflow" aria-label="Recording workflow"><li><span>1</span> Upload audio</li><li><span>2</span> Transcribe</li><li><span>3</span> Import events</li></ol></div><div class="voice-upload-bar"><input id="voice-upload" type="file" accept=".m4a,.mp3,.wav" hidden><div class="voice-file-picker"><button id="voice-choose" class="secondary">Choose audio file</button><div class="voice-file-selection"><span id="voice-filename">No audio selected</span><small>M4A, MP3 or WAV · Local transcription</small></div></div><button id="voice-upload-button" disabled>Upload recording</button></div><div id="voice-status" class="voice-notice" role="status" hidden></div><div id="voice-recordings"></div><div id="voice-unmatched"></div>`;
    $("voice-choose").onclick=()=>$("voice-upload").click();
    $("voice-upload").onchange=()=>{const file=$("voice-upload").files[0];$("voice-filename").textContent=file?.name||"No audio selected";$("voice-filename").title=file?.name||"";$("voice-upload-button").disabled=!voiceAvailable||!file};
    $("voice-upload-button").onclick=async()=>{const file=$("voice-upload").files[0];if(!file)return;const button=$("voice-upload-button");button.disabled=true;
      button.textContent="Uploading…";voiceNotice("");
      try{await api(`/api/runs/${id}/recordings`,{method:"POST",headers:{"X-Filename":encodeURIComponent(file.name)},body:file});if(state.run.id===id){voiceNotice("Recording uploaded. Confirm the start time, then transcribe.","success");$("voice-upload").value="";$("voice-filename").textContent="No audio selected";$("voice-filename").title="";loadRecordings(id)}}catch(err){if(state.run.id===id)voiceNotice(err.message,"error")}finally{button.textContent="Upload recording";button.disabled=!voiceAvailable||!$("voice-upload")?.files[0]}};
  }
  const unmatched=visibleEvents().filter(e=>e.source==="voice"&&!e.run_file_id);
  $("voice-unmatched").innerHTML=unmatched.length?`<details class="voice-unmatched-list"><summary>Unmatched events <span>${unmatched.length}</span></summary>${unmatched.map(e=>`<div class="voice-unmatched-row"><div><small>${esc(voiceTime(Number(e.timestamp_ns)/1e6))}</small><b>${esc(eventLabel(e))}</b><p>${esc(eventName(e))}</p><small>${esc(eventMetadata(e))}</small></div><button class="secondary" data-evidence="${e.id}">Review audio</button></div>`).join("")}</details>`:'<div class="voice-unmatched-empty"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="10" cy="10" r="7"/><path d="m6.5 10 2.3 2.3 4.7-4.8"/></svg>No unmatched events</div>';
  $("voice-unmatched").querySelectorAll("[data-evidence]").forEach(b=>b.onclick=()=>openVoiceEvidence(state.timeline.events.find(e=>e.id===Number(b.dataset.evidence))));
  api("/api/capabilities").then(capabilities=>{
    if(state.run?.id!==id)return;
    voiceAvailable=capabilities.local_transcription.available;
    $("voice-choose").disabled=!voiceAvailable;
    $("voice-upload-button").disabled=!voiceAvailable||!$("voice-upload").files[0];
    if(!voiceAvailable)voiceNotice(capabilities.local_transcription.message);
    panel.querySelectorAll("[data-transcribe]").forEach(button=>{if(!voiceAvailable)button.disabled=true});
  }).catch(error=>voiceNotice(error.message,"error"));
  loadRecordings(id);
}

async function loadRecordings(id){
  const request=++voiceRequest;clearTimeout(voicePoll);
  try{const rows=await api(`/api/runs/${id}/recordings`);if(state.run?.id!==id||request!==voiceRequest)return;
    const container=$("voice-recordings");
    // Preserve expanded time settings and edits when polling updates the cards.
    const structuralRow=r=>{const {processed_ms,...fields}=r;return JSON.stringify(fields)};
    const signature=JSON.stringify(rows.map(structuralRow));if(container.dataset.signature!==signature){
      const settings=new Map([...container.querySelectorAll("[data-recording]")].map(card=>{const input=card.querySelector("[data-start]");return [card.dataset.recording,{open:card.querySelector("details").open,dirty:input.dataset.dirty,value:input.value}]}));
      const focused=document.activeElement,focusId=focused?.dataset?.start,summaryId=focused?.tagName==="SUMMARY"?focused.closest("[data-recording]")?.dataset.recording:null,selection=focusId?[focused.selectionStart,focused.selectionEnd]:null;
      // Keep unchanged cards mounted so another recording's progress cannot close a file chooser.
      const existing=new Map([...container.querySelectorAll("[data-recording]")].map(card=>[card.dataset.recording,card]));
      rows.forEach((r,index)=>{let card=existing.get(r.id);const rowSignature=structuralRow(r);if(card?.dataset.signature!==rowSignature){const holder=document.createElement("div");holder.innerHTML=recordingCard(r);const replacement=holder.firstElementChild;replacement.dataset.signature=rowSignature;if(card)card.replaceWith(replacement);card=replacement}const position=container.children[index];if(position!==card)container.insertBefore(card,position||null);existing.delete(r.id)});
      existing.forEach(card=>card.remove());container.dataset.signature=signature;
      container.querySelectorAll("[data-recording]").forEach(card=>{const previous=settings.get(card.dataset.recording),input=card.querySelector("[data-start]");if(previous){card.querySelector("details").open=previous.open;if(previous.dirty&&!input.disabled){input.value=previous.value;input.dataset.dirty="true"}}input.oninput=()=>input.dataset.dirty="true"});
      if(focusId){const input=container.querySelector(`[data-start="${focusId}"]`);if(input&&!input.disabled){input.focus({preventScroll:true});input.setSelectionRange(...selection)}}
      else if(summaryId)container.querySelector(`[data-recording="${summaryId}"] summary`)?.focus({preventScroll:true});
      container.querySelectorAll("[data-choose-events]").forEach(b=>b.onclick=()=>container.querySelector(`[data-events="${b.dataset.chooseEvents}"]`).click());
      container.querySelectorAll("[data-transcribe]").forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api(`/api/recordings/${b.dataset.transcribe}/transcribe`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({start_time:container.querySelector(`[data-start="${b.dataset.transcribe}"]`).value})});if(state.run?.id!==id)return;voiceNotice("");loadRecordings(id)}catch(e){b.disabled=false;if(state.run?.id===id)voiceNotice(e.message,"error")}});
      container.querySelectorAll("[data-events]").forEach(input=>input.onchange=async()=>{if(!input.files[0])return;try{const payload=JSON.parse(await input.files[0].text());const result=await api(`/api/recordings/${input.dataset.events}/events`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});if(state.run?.id!==id)return;voiceNotice(`Imported ${result.added} events; skipped ${result.skipped} duplicates.`,"success");await refreshEventViews()}catch(e){if(state.run?.id===id)voiceNotice(e.message,"error")}finally{input.value=""}});
    }
    rows.forEach(r=>{const card=container.querySelector(`[data-recording="${r.id}"]`),progress=card?.querySelector("progress");if(!progress)return;card.querySelector("[data-progress-time]").textContent=`${voiceDuration(r.processed_ms)} / ${voiceDuration(r.duration_ms)}`;if(r.duration_ms&&r.stage==="Transcribing")progress.value=Math.min(r.duration_ms,r.processed_ms||0)});
    if(rows.some(r=>["queued","processing"].includes(r.status)))voicePoll=setTimeout(()=>loadRecordings(id),2000);
  }catch(e){if(state.run?.id===id)voiceNotice(e.message,"error")}
}

function voiceReportMarkup(e){
  const v=e.voice,candidates=e.link_candidates||[];
  const matchLabel=voiceMatchLabel(v);
  return `<section class="voice-report">
    <div class="voice-report-heading"><h3>${esc(v.title)}</h3><span class="voice-assessment" data-assessment="${esc(v.category)}">${esc(voiceCategoryLabel(v.category))}</span></div>
    <div class="voice-report-meta">Category: ${esc(reasonLabel(eventCategory(e)))}</div>
    <div class="voice-field"><span class="voice-field-label">Description</span><div class="voice-description voice-readonly">${esc(e.user_description||e.auto_description||"")}</div></div>
    <div class="voice-field"><span class="voice-field-label">Audio evidence</span><div class="voice-audio-evidence"><div class="voice-clip-range">${esc(voiceTime(v.absolute_start))} – ${esc(voiceTime(v.absolute_end))}</div><audio controls preload="metadata" data-report="${e.id}" src="${esc(v.audio_url)}"></audio><div class="voice-playback-clock"><span>Playback time</span><strong data-audio-clock="${e.id}">${esc(voiceTime(new Date(v.absolute_start).getTime()-Math.min(2000,v.start_ms)))}</strong></div></div></div>
    <details class="voice-transcript-details"><summary>Transcript</summary><pre class="voice-transcript">${esc(v.transcript)}</pre></details>
    <fieldset class="voice-association"><legend>Automatic exit association</legend><div class="voice-association-summary"><span class="voice-match-status" data-status="${esc(v.match_status)}">${esc(matchLabel)}</span>${Number.isFinite(v.delta_s)?`<span class="voice-report-delay">Report delay: ${v.delta_s.toFixed(2)} s</span>`:""}</div>
      <label>Automatic exit<select data-target="${e.id}"><option value="">Select exit</option>${candidates.map(c=>`<option value="${c.id}" ${v.linked_event_id===c.id?"selected":""}>${esc(voiceTime(Number(c.timestamp_ns)/1e6))} · ${esc(eventLabel(c))}</option>`).join("")}</select></label>
      ${!candidates.length?'<p class="voice-association-hint">No automatic exits are available in this Run.</p>':""}
      <div class="voice-association-actions"><button class="secondary" data-unlink="${e.id}">Unlink</button><button class="secondary" data-rematch="${e.id}">Match automatically</button><button data-link="${e.id}" ${v.linked_event_id===null||!candidates.some(c=>c.id===v.linked_event_id)?"disabled":""}>Link</button></div>
    </fieldset><footer class="voice-report-actions"><button class="secondary" data-edit-voice="${e.id}">Edit report</button></footer>
  </section>`;
}
function openVoiceEvidence(event){
  const modal=$("voice-modal");if(modal.hidden)voiceReturnFocus=document.activeElement;
  modal.querySelectorAll("audio").forEach(audio=>audio.pause());modal.hidden=false;
  const reports=event.voice?[event]:event.voice_reports||[];
  $("voice-evidence").innerHTML=`<div id="voice-feedback" role="status" hidden></div><div class="voice-event-summary"><div class="voice-field"><span class="voice-field-label">Event time</span><div class="voice-readonly">${esc(voiceTime(Number(event.timestamp_ns)/1e6))}</div></div><div class="voice-field"><span class="voice-field-label">Category</span><div class="voice-readonly">${esc(reasonLabel(eventCategory(event)))}</div></div></div>${!event.voice?'<div class="voice-event-actions"><button id="voice-edit-main" class="secondary">Edit event</button></div>':""}`+reports.map(voiceReportMarkup).join("");
  $("voice-evidence").scrollTop=0;$("voice-close").focus({preventScroll:true});
  const editMain=$("voice-edit-main");if(editMain)editMain.onclick=()=>editVoiceEvent(event);
  $("voice-evidence").querySelectorAll("audio").forEach(audio=>{
    const e=reports.find(r=>r.id===Number(audio.dataset.report)),v=e.voice,start=Math.max(0,v.start_ms/1000-2),end=Math.min(v.duration_ms/1000,v.end_ms/1000+2);
    audio.onloadedmetadata=()=>audio.currentTime=start;
    audio.onseeked=()=>{if(audio.currentTime<start)audio.currentTime=start;if(audio.currentTime>end)audio.currentTime=end};
    audio.onplay=()=>{for(const other of $("voice-evidence").querySelectorAll("audio"))if(other!==audio)other.pause();if(audio.currentTime<start||audio.currentTime>=end)audio.currentTime=start};
    audio.ontimeupdate=()=>{if(audio.currentTime>=end)audio.pause();const base=new Date(v.absolute_start).getTime()-v.start_ms;$("voice-evidence").querySelector(`[data-audio-clock="${e.id}"]`).textContent=voiceTime(base+audio.currentTime*1000)};
  });
  $("voice-evidence").querySelectorAll("[data-edit-voice]").forEach(b=>b.onclick=()=>editVoiceEvent(reports.find(e=>e.id===Number(b.dataset.editVoice))));
  $("voice-evidence").querySelectorAll("[data-target]").forEach(select=>select.onchange=()=>{$("voice-evidence").querySelector(`[data-link="${select.dataset.target}"]`).disabled=!select.value});
  for(const [attribute,mode] of [["link","manual"],["unlink","disabled"],["rematch","auto"]])$("voice-evidence").querySelectorAll(`[data-${attribute}]`).forEach(b=>b.onclick=async()=>{
    const id=Number(b.dataset[attribute]),target=$("voice-evidence").querySelector(`[data-target="${id}"]`).value;
    if(mode==="manual"&&!target)return;
    b.disabled=true;
    try{await api(`/api/events/${id}/link`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({mode,target_id:mode==="manual"?Number(target):null})});$("voice-close").click();await refreshEventViews()}catch(e){const feedback=$("voice-feedback");feedback.textContent=e.message;feedback.dataset.tone="error";feedback.hidden=false;feedback.scrollIntoView({block:"nearest"});b.disabled=false}
  });
}
function editVoiceEvent(event){$("voice-close").click();openEventEditor(event,state.run.files.find(f=>f.id===event.run_file_id));if(event.source==="voice")$("event-description").value=event.user_description||event.auto_description||"";$("event-close").focus({preventScroll:true})}
