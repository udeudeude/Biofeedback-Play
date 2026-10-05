// Run with node tests/test_ui.js. Exercise embedded graph/audio logic without hardware.
const {spawnSync} = require('node:child_process');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const data = JSON.parse(spawnSync('python', ['-c', 'import json,re; from biofeedback_play import HTML,SIGNAL_DEFINITIONS; print(json.dumps({"script":re.findall(r"<script>(.*?)</script>",HTML,re.S)[-1],"signals":SIGNAL_DEFINITIONS}))'], {encoding:'utf8'}).stdout);
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    id, value:id === 'masterVolume' ? '35' : '', dataset:{}, innerHTML:'', textContent:'',
    style:{setProperty(){}}, classList:{add(){},remove(){},toggle(){},contains(){return true;}},
    setAttribute(){}, addEventListener(){}, querySelectorAll(){return [];},
    getBoundingClientRect(){return {width:400,height:150,top:0,bottom:150};}
  });
  return nodes.get(id);
}
const params = () => ({value:0,setTargetAtTime(v){this.value=v;}});
class AudioContext {
  constructor(){this.currentTime=0;this.destination={};}
  resume(){}
  createGain(){return {gain:params(),connect(){return this;}};}
  createOscillator(){return {frequency:params(),connect(){return this;},start(){this.started=true;},stop(){this.stopped=true;}};}
}
const context=vm.createContext({console,document:{hidden:false,activeElement:null,
  documentElement:{dataset:{},style:{setProperty(){}}},getElementById:node,
  querySelectorAll(){return [];},addEventListener(){}},
  window:{AudioContext,devicePixelRatio:1,innerHeight:900,addEventListener(){}},
  localStorage:{getItem(){return null;},setItem(){}},
  navigator:{},requestAnimationFrame(){},setInterval(){},setTimeout(){},
  getComputedStyle(){return {getPropertyValue(){return '#cccccc';}};},
  fetch(){throw new Error('Unexpected network request');}
});
let script=data.script.replace(/refreshAll\(\);\s*refreshMusePorts\(\);\s*scanDevices\(\);\s*setInterval\(refreshAll, 1000\);\s*setInterval\(pollSignals, 200\);/, '');
vm.runInContext(script,context);
function run(code){return vm.runInContext(code,context);}
context.signals=Object.entries(data.signals).filter(([id])=>id.startsWith('muse.')).map(([id,s])=>({...s,id,connected:true,running:true,device_name:'Muse',sample_count:10}));
run('catalog = {devices:[{id:"muse",name:"Muse",connected:true,running:true}], signals};');
run('renderSignalPanels(signals)');
assert(node('signalGrid').innerHTML.includes('<details class="band-details" id="bandDetails"><summary>'));
for(const band of ['delta','theta','alpha','beta','gamma']) {
  assert(node('signalGrid').innerHTML.includes(`data-band-audio="muse.band.${band}"`));
  const id=`muse.band.${band}`;
  run(`ensureSignalState(catalog.signals.find(s=>s.id==='${id}')).values=[1,10,100];toggleAudio('${id}');`);
  assert(run(`signalState['${id}'].audioNode.oscillator.started`));
  assert(run(`signalState['${id}'].audioNode.oscillator.frequency.value`) > 180);
}
run('document.getElementById("muteAll").onclick()');
assert(run('Object.values(signalState).every(s=>!s.audioOn)'));
const points=run('graphPoints([0,0,90,0,0,0,0,-60,0,0],[0,.01,.2,.3,.4,.5,.6,2,2.1,3],6)');
assert(points.some(p=>p.v===90 && p.t===.2));
assert(points.some(p=>p.v===-60 && p.t===2));
assert(points.every((p,i)=>i===0 || p.t>=points[i-1].t));
const values=Array.from({length:100},(_,i)=>i===99 ? 1000000 : i);
context.rangeValues=values;
assert(run('graphRange(rangeValues,true).max') < 1000);
run('document.getElementById("oscHost").oninput()');
assert(run('oscFormDirty'));
run("panelOrder=['muse.band.gamma','muse.band.alpha'];");
assert.equal(run('visibleSignals(catalog.signals)[0].id'), 'muse.band.gamma');
console.log('UI logic: five-band audio with collapsed cards, mute, timestamp-preserving peaks, robust range, and OSC editing passed.');
