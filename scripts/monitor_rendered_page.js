// Deterministic DOM visibility scanner, evaluated in each rendered frame.
(async function() {
  const visible = el => {
    for (let p = el; p; p = p.parentElement || p.getRootNode().host) {
      const s = getComputedStyle(p);
      if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0 || p.hidden) return false;
    }
    return [...el.getClientRects()].some(r => r.width > 0 && r.height > 0 && r.right > 0 &&
      r.left < document.documentElement.clientWidth && r.bottom > 0 && r.top < document.documentElement.scrollHeight);
  };
  const roots = [document.body];
  for(let index=0;index<roots.length;index++) {
    for(const el of roots[index].querySelectorAll('*')) if(el.shadowRoot) roots.push(el.shadowRoot);
  }
  const text = [];
  for(const root of roots) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (n.parentElement && !['SCRIPT','STYLE','NOSCRIPT'].includes(n.parentElement.tagName) && visible(n.parentElement)) {
      const value = n.textContent.trim(); if (value) text.push(value);
    }
  }
  }
  const elements = roots.flatMap(root=>[...root.querySelectorAll('*')]);
  const bounds = el => {const r=el.getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height};};
  const images = elements.filter(el=>el.tagName==='IMG').filter(visible)
    .map(el => ({src:el.currentSrc,alt:el.alt,loaded:el.complete && el.naturalWidth > 0,
      bounds:bounds(el),natural_width:el.naturalWidth,natural_height:el.naturalHeight}));
  const regions=images.map(i=>({kind:'image',src:i.src,...i.bounds}));
  const backgroundUrls=new Set();
  let generated_content=false;
  for(const el of elements.filter(visible)) {
    for(const pseudo of [null,'::before','::after']) {
      const style=getComputedStyle(el,pseudo);
      for(const match of style.backgroundImage.matchAll(/url\(["']?(.*?)["']?\)/g)) {
        try {backgroundUrls.add(new URL(match[1],document.baseURI).href);} catch(e) {backgroundUrls.add('unresolved_background');}
        regions.push({kind:'background',src:match[1],...bounds(el)});
      }
      if(pseudo && !['none','normal','""',"''"].includes(style.content)) {
        generated_content=true;regions.push({kind:'generated',...bounds(el)});
      }
    }
  }
  const backgrounds=[];
  for(const src of backgroundUrls) {
    const image=new Image();image.src=src;
    let decoded=false;
    try {decoded=await Promise.race([image.decode().then(()=>true),new Promise(r=>setTimeout(()=>r(false),2000))]);} catch(e) {}
    backgrounds.push({src,decoded});
  }
  const unreviewed_regions = elements.filter(visible).filter(el=> {
    if(el===document.body || el===document.documentElement) return false;
    const s=getComputedStyle(el);
    return (el.tagName.includes('-') && !el.shadowRoot) ||
      (/auto|scroll|hidden|clip/.test(s.overflowY) && el.scrollHeight>el.clientHeight+2) ||
      (/auto|scroll|hidden|clip/.test(s.overflowX) && el.scrollWidth>el.clientWidth+2);
  }).length;
  const canvases=elements.filter(el=>el.tagName==='CANVAS').filter(visible);
  regions.push(...canvases.map(el=>({kind:'canvas',...bounds(el)})));
  const obscured=elements.filter(visible).some(el=>{const s=getComputedStyle(el),r=el.getBoundingClientRect();
    return s.position==='fixed' && r.width*r.height>innerWidth*innerHeight*.35 && Number(s.zIndex)>0;});
  return {text:text.join('\n'), images, backgrounds, regions, obscured, generated_content,unreviewed_regions,canvas: canvases.length,
    height:document.documentElement.scrollHeight, ready:document.readyState === 'complete'};
})
