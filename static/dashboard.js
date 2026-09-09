
(function(){
  var DATA = JSON.parse(document.getElementById('viz-data').textContent);
  var LIVE = !!DATA.live;
  var history, forecasts, meta, compareData, inventoryData, inventoryRaw, dailyData;
  var products, brandOrder, HIST_MONTHS, newProducts = [];

  function fmt(n){ return (n==null) ? '-' : Math.round(n).toLocaleString('ko-KR'); }
  function fmtCompact(n){
    var abs = Math.abs(n);
    if (abs>=10000) return (n/10000).toFixed(1).replace(/\.0$/,'')+'만';
    if (abs>=1000) return (n/1000).toFixed(1).replace(/\.0$/,'')+'천';
    return Math.round(n).toLocaleString('ko-KR');
  }
  function escapeXML(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function niceCeil(v){
    if (v<=0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log10(v)));
    var norm = v/mag, n;
    if (norm<=1) n=1; else if (norm<=2) n=2; else if (norm<=5) n=5; else n=10;
    return n*mag;
  }

  var toastEl = document.getElementById('toast');
  var toastTimer = null;
  function showToast(msg, isError){
    toastEl.textContent = msg;
    toastEl.className = 'toast' + (isError ? ' error' : '');
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function(){ toastEl.hidden = true; }, 2600);
  }

  function postJSON(path, body){
    return fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function(res){
      return res.json().catch(function(){ return {}; }).then(function(json){
        if (!res.ok) throw new Error(json.error || ('저장 실패 (HTTP '+res.status+')'));
        return json;
      });
    }, function(){
      throw new Error('서버에 연결할 수 없어요.');
    });
  }

  function brandOf(p){ return (meta[p] && meta[p].brand) || '미분류'; }

  var SERIES_VARS = ['--series-1','--series-2','--series-3','--series-4','--series-5','--series-6'];
  // 브랜드는 이름으로 색을 고정 (순번이 바뀌어도 색이 안 흔들리게)
  var BRAND_VARS = {
    '보르헤스(포스티보나)': '--series-1',            // 파랑
    '산체스(소르바스)': '--series-2',                // 초록
    '에스파뇰라 스프레이': '--series-3',             // 노랑
    '에스테파(포스티보나 프리미엄)': '--series-5',   // 빨강
    '에스파뇰라': '--series-6'                       // 보라
  };
  function brandColor(i){ return 'var(' + SERIES_VARS[i % SERIES_VARS.length] + ')'; }
  function colorOfBrand(b, i){
    return 'var(' + (BRAND_VARS[b] || SERIES_VARS[i % SERIES_VARS.length]) + ')';
  }

  var activeBrands = {};
  var searchTerm = '';
  var selectedProduct = null;
  var fcStateByProduct = {};
  var fcPreview = null;
  var fcUnlocked = false;      // 네온 예측선을 끌어서 고칠 수 있는 상태인가
  var fcCustomOpen = false;    // '직접설정' 패널(방식·배율 등 입력칸)을 펼쳐놨는가
  var fcManual = {};           // {상품: {연월: 직접 끌어서 정한 값}}
  var fcOnManual = null;       // 끌기 끝났을 때 패널 전체 갱신 (renderForecastPanel 이 채움)
  var fcOnManualLive = null;   // 끄는 중 가벼운 갱신

  function currentYM(){
    var d = new Date();
    var mm = d.getMonth()+1;
    return d.getFullYear() + '-' + (mm<10?'0':'')+mm;
  }

  function applyData(data){
    DATA = data;
    history = DATA.history; forecasts = DATA.forecasts; meta = DATA.meta;
    compareData = DATA.compare; inventoryData = DATA.inventory || {};
    inventoryRaw = DATA.inventory_raw || {};
    dailyData = DATA.daily || {};

    // 신상품(meta.new)은 수요예측 화면에서 빼고 '신상품관리' 탭에서만 다룬다.
    // 실적이 몇 달뿐이라 예측·계절성·브랜드비중을 왜곡하기 때문.
    // 신상품은 meta 기준으로 잡는다 — 방금 등록해서 아직 실적이 없는 제품도 보이게.
    var allKeys = Object.keys(history).sort();
    newProducts = Object.keys(meta).filter(function(p){ return meta[p] && meta[p]['new']; }).sort();
    products = allKeys.filter(function(p){ return !(meta[p] && meta[p]['new']); });

    brandOrder = [];
    products.forEach(function(p){ var b = brandOf(p); if (brandOrder.indexOf(b)===-1) brandOrder.push(b); });
    brandOrder.forEach(function(b){ if (!(b in activeBrands)) activeBrands[b] = true; });

    var allMonthsSet = {};
    products.forEach(function(p){ Object.keys(history[p]).forEach(function(m){ allMonthsSet[m]=true; }); });
    HIST_MONTHS = Object.keys(allMonthsSet).sort();

    if ((!selectedProduct || products.indexOf(selectedProduct)===-1) && products.length){
      selectedProduct = products[0];
    }
    renderAll();
  }

  var themeBtn = document.getElementById('themeToggle');
  function applyThemeLabel(){
    var cur = document.documentElement.getAttribute('data-theme');
    themeBtn.textContent = cur === 'dark' ? '라이트 모드' : '다크 모드';
  }
  // 이커머스 화면은 iframe 이라 테마가 자동으로 안 따라온다 — 바뀔 때마다 알려준다
  function tellEcomTheme(){
    var fr = document.getElementById('ecomFrame');
    if (!fr || !fr.contentWindow) return;
    var cur = document.documentElement.getAttribute('data-theme');
    if (!cur) cur = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    try { fr.contentWindow.postMessage({type:'theme', theme:cur}, '*'); } catch(e){}
  }
  // 기본값은 다크. 한 번 바꾸면 그 선택을 기억한다 (예전엔 새로고침하면 되돌아갔다).
  try {
    var savedTheme = localStorage.getItem('ub_theme');
    if (savedTheme === 'light' || savedTheme === 'dark'){
      document.documentElement.setAttribute('data-theme', savedTheme);
    }
  } catch(e){}
  themeBtn.addEventListener('click', function(){
    var cur = document.documentElement.getAttribute('data-theme');
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('ub_theme', next); } catch(e){}
    applyThemeLabel();
    tellEcomTheme();
  });
  applyThemeLabel();
  // iframe 이 늦게 뜨므로, 뜬 뒤에도 한 번 알려준다
  window.addEventListener('message', function(e){
    if (e.data && e.data.type === 'ecom-ready') tellEcomTheme();
  });

  var brandFilterEl = document.getElementById('brandFilter');
  function activeCount(){ return brandOrder.filter(function(b){return activeBrands[b];}).length; }
  function renderBrandFilter(){
    brandFilterEl.innerHTML = '';
    var allChip = document.createElement('button');
    allChip.type='button'; allChip.className = 'chip' + (activeCount()===brandOrder.length ? ' active':'');
    allChip.textContent='전체';
    allChip.addEventListener('click', function(){
      var allOn = activeCount()===brandOrder.length;
      // 다 켜져 있으면 전부 끄고(그다음 브랜드별로 선택), 아니면 전부 켜기
      brandOrder.forEach(function(b){ activeBrands[b] = !allOn; });
      renderAll();
    });
    brandFilterEl.appendChild(allChip);
    brandOrder.forEach(function(b){
      var chip = document.createElement('button');
      chip.type='button';
      chip.className='chip'+(activeBrands[b]?' active':'');
      chip.textContent=b;
      chip.addEventListener('click', function(){
        activeBrands[b] = !activeBrands[b];
        renderAll();
      });
      brandFilterEl.appendChild(chip);
    });
  }

  document.getElementById('productSearch').addEventListener('input', function(e){
    searchTerm = e.target.value.trim().toLowerCase();
    renderSparkGrid();
  });

  // 상품별 개요 가로 릴스 — 드래그로 스크롤 (드래그와 클릭 구분)
  var sparkDrag = {down:false, moved:false, startX:0, startScroll:0};
  (function(){
    var gridEl = document.getElementById('sparkGrid');
    gridEl.addEventListener('pointerdown', function(e){
      sparkDrag.down=true; sparkDrag.moved=false;
      sparkDrag.startX=e.clientX; sparkDrag.startScroll=gridEl.scrollLeft;
    });
    window.addEventListener('pointermove', function(e){
      if(!sparkDrag.down) return;
      var dx=e.clientX-sparkDrag.startX;
      if(Math.abs(dx)>5){ sparkDrag.moved=true; gridEl.classList.add('dragging'); }
      if(sparkDrag.moved){ gridEl.scrollLeft = sparkDrag.startScroll - dx; }
    });
    window.addEventListener('pointerup', function(){
      if(!sparkDrag.down) return;
      sparkDrag.down=false; gridEl.classList.remove('dragging');
      setTimeout(function(){ sparkDrag.moved=false; }, 0);
    });
    gridEl.addEventListener('wheel', function(e){
      if(e.deltaY!==0 && Math.abs(e.deltaY)>Math.abs(e.deltaX)){ gridEl.scrollLeft += e.deltaY; e.preventDefault(); }
    }, {passive:false});
  })();

  function kpiTile(label, value, extra){
    var el = document.createElement('div'); el.className='kpi-tile';
    var l = document.createElement('div'); l.className='kpi-label'; l.textContent=label;
    var v = document.createElement('div'); v.className='kpi-value'; v.textContent=value;
    el.appendChild(l); el.appendChild(v);
    if (extra){
      var d = document.createElement('div'); d.className='kpi-delta '+(extra.cls||''); d.textContent=extra.text;
      el.appendChild(d);
    }
    return el;
  }

  function renderKPIs(){
    var visible = products.filter(function(p){ return activeBrands[brandOf(p)]; });
    var latestMonth = HIST_MONTHS[HIST_MONTHS.length-1];
    var prevMonth = HIST_MONTHS[HIST_MONTHS.length-2];
    var latestTotal=0, prevTotal=0;
    visible.forEach(function(p){
      if (history[p][latestMonth]!=null) latestTotal += history[p][latestMonth];
      if (prevMonth && history[p][prevMonth]!=null) prevTotal += history[p][prevMonth];
    });
    var delta = prevTotal ? ((latestTotal-prevTotal)/prevTotal*100) : null;
    var mover=null;
    visible.forEach(function(p){
      var a=history[p][latestMonth], b=prevMonth?history[p][prevMonth]:null;
      if (a!=null && b){ var chg=(a-b)/b*100; if(!mover||Math.abs(chg)>Math.abs(mover.chg)) mover={p:p,chg:chg}; }
    });
    var row = document.getElementById('kpiRow');
    row.innerHTML='';
    row.appendChild(kpiTile('표시 중인 SKU', visible.length+'개'));
    row.appendChild(kpiTile((latestMonth||'-')+' 총 실적', fmt(latestTotal)+'개'));
    row.appendChild(kpiTile('전월 대비', delta==null?'-':(delta>=0?'+':'')+delta.toFixed(1)+'%', delta==null?null:{cls:delta>=0?'pos':'neg', text: prevMonth?('vs '+prevMonth):''}));
    row.appendChild(kpiTile('최대 변동 상품', mover?mover.p:'-', mover?{cls:mover.chg>=0?'pos':'neg', text:(mover.chg>=0?'+':'')+mover.chg.toFixed(1)+'%'}:null));
    document.getElementById('sparkCount').textContent = '('+visible.length+'개 표시 중)';
  }

  function renderLineChart(container, series, months, opts){
    opts = opts || {};
    var W = 900, H = opts.height || 280;
    var bars = (opts.bars && Object.keys(opts.bars.map||{}).length) ? opts.bars : null;
    var padL = 56, padR = bars ? 46 : 16, padT = 16, padB = 28;
    var plotW = W - padL - padR, plotH = H - padT - padB;

    var hasData = months.length && series.some(function(s){ return Object.keys(s.values).length; });
    if (!hasData){ container.innerHTML = '<div class="empty-note">표시할 데이터가 없어요</div>'; return; }

    var allVals = [];
    series.forEach(function(s){ months.forEach(function(m){ if (s.values[m]!=null) allVals.push(s.values[m]); }); });
    var maxV = allVals.length ? Math.max.apply(null, allVals) : 1;
    var niceMax = niceCeil(maxV || 1);

    function xAt(i){ return months.length<=1 ? padL : padL + (i/(months.length-1))*plotW; }
    function yAt(v){ return padT + plotH - (Math.max(0,v)/niceMax)*plotH; }

    var svg = '<svg viewBox="0 0 '+W+' '+H+'" class="chart-svg" preserveAspectRatio="none">';
    if (series.some(function(s){ return s.glow; })){
      svg += '<defs><filter id="neonGlow" x="-30%" y="-30%" width="160%" height="160%">'+
        '<feGaussianBlur stdDeviation="2.6" result="b"/>'+
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>';
    }
    var steps = 4;
    for (var s=0;s<=steps;s++){
      var v = niceMax*s/steps;
      var y = yAt(v);
      svg += '<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" class="gridline"/>';
      svg += '<text x="'+(padL-8)+'" y="'+(y+3)+'" class="axis-label" text-anchor="end">'+fmtCompact(v)+'</text>';
    }
    svg += '<line x1="'+padL+'" y1="'+yAt(0)+'" x2="'+(W-padR)+'" y2="'+yAt(0)+'" class="baseline"/>';

    // 재고 막대 오버레이 — 오른쪽 축 기준, 흐리게, 선그래프 뒤에 깔림
    if (bars){
      var barNiceMax = niceCeil(bars.max || 1);
      var ybar = function(v){ return padT + plotH - (Math.max(0,v)/barNiceMax)*plotH; };
      var bslot = plotW / months.length;
      var bW = Math.min(26, bslot*0.55);
      months.forEach(function(m,i){
        var b = bars.map[m]; if(!b) return;
        var bx = xAt(i) - bW/2;
        var baseTop = ybar(b.base), zeroY = ybar(0);
        // 예측 반영분은 네온그린 + 점선 테두리로 실제 재고와 구분한다
        var fillC = b.projected ? 'var(--fc-neon)' : 'var(--series-1)';
        var fillO = b.projected ? 0.20 : 0.14;
        if (b.projected && b.close < 0){ fillC = 'var(--series-5)'; fillO = 0.34; }
        svg += '<rect x="'+bx.toFixed(1)+'" y="'+baseTop.toFixed(1)+'" width="'+bW.toFixed(1)+'" height="'+(zeroY-baseTop).toFixed(1)+
               '" fill="'+fillC+'" fill-opacity="'+fillO+'"'+
               (b.projected?' stroke="'+fillC+'" stroke-opacity="0.55" stroke-width="1" stroke-dasharray="3 2"':'')+'/>';
        if (b.inbound){
          // 입고량이 작아도 최소 2px 은 보이게 (막대가 안 보인다는 얘기가 없도록)
          var totTop = ybar(b.qty);
          var ih = Math.max(2, baseTop - totTop);
          svg += '<rect x="'+bx.toFixed(1)+'" y="'+(baseTop-ih).toFixed(1)+'" width="'+bW.toFixed(1)+'" height="'+ih.toFixed(1)+'" fill="var(--series-3)" fill-opacity="0.30"/>';
        }
      });
      for (var bs=0;bs<=steps;bs++){
        var bv = barNiceMax*bs/steps, by = ybar(bv);
        svg += '<text x="'+(W-padR+6)+'" y="'+(by+3)+'" class="axis-label" text-anchor="start">'+fmtCompact(bv)+'</text>';
      }
    }

    var tickEvery = Math.max(1, Math.ceil(months.length/7));
    months.forEach(function(m,i){
      if (i%tickEvery===0 || i===months.length-1){
        svg += '<text x="'+xAt(i)+'" y="'+(H-8)+'" class="axis-label" text-anchor="middle">'+m.slice(2)+'</text>';
      }
    });

    series.forEach(function(s){
      var d=''; var started=false;
      months.forEach(function(m,i){
        var v = s.values[m];
        if (v==null){ started=false; return; }
        var x=xAt(i), y=yAt(v);
        d += (started?'L':'M')+x.toFixed(2)+','+y.toFixed(2)+' ';
        started=true;
      });
      if (!d) return;
      svg += '<path d="'+d.trim()+'" fill="none" stroke="'+s.color+'" stroke-width="'+(s.glow?2.4:2)+'" '+(s.dashed?'stroke-dasharray="6 4"':'')+
        (s.glow?' filter="url(#neonGlow)"':'')+' stroke-linecap="round" stroke-linejoin="round"/>';
      // dots:true 면 모든 데이터 점에 마커를 찍는다 (예측선처럼 월별 값을 짚어봐야 할 때)
      if (s.dots){
        months.forEach(function(m,i){
          if (s.values[m]==null) return;
          var cx=xAt(i).toFixed(2), cy=yAt(s.values[m]).toFixed(2);
          if (s.draggable){
            // 잠금해제: 잡기 쉽게 크게 + 흰 테두리 링으로 확실히 보이게
            svg += '<circle class="drag-halo" cx="'+cx+'" cy="'+cy+'" r="10" fill="'+s.color+'" fill-opacity="0.18"/>'+
                   '<circle class="drag-dot" data-m="'+m+'" cx="'+cx+'" cy="'+cy+
                   '" r="6" fill="'+s.color+'" stroke="var(--surface-1)" stroke-width="2.5"/>';
          } else {
            svg += '<circle cx="'+cx+'" cy="'+cy+'" r="3.5" fill="'+s.color+'"'+
              ' stroke="var(--surface-1)" stroke-width="1.5"'+(s.glow?' filter="url(#neonGlow)"':'')+'/>';
          }
        });
      }
      for (var i=months.length-1;i>=0;i--){
        if (s.values[months[i]]!=null){
          var x=xAt(i), y=yAt(s.values[months[i]]);
          svg += '<circle cx="'+x+'" cy="'+y+'" r="4" fill="'+s.color+'" stroke="var(--surface-1)" stroke-width="2"/>';
          if (opts.directLabel!==false && series.length<=4){
            svg += '<text x="'+(x+8)+'" y="'+(y+3)+'" class="direct-label" fill="var(--text-primary)">'+escapeXML(s.name)+'</text>';
          }
          break;
        }
      }
    });
    svg += '</svg>';

    container.innerHTML = '<div class="chart-wrap"><div class="chart-tooltip" hidden></div></div>';
    var wrap = container.querySelector('.chart-wrap');
    wrap.insertAdjacentHTML('afterbegin', svg);
    var svgEl = wrap.querySelector('svg');
    var tooltip = wrap.querySelector('.chart-tooltip');

    // ── 예측 점 끌어서 고치기 (잠금해제 상태에서만 .drag-dot 이 생김) ──
    var dragging = false;
    if (opts.onDrag){
      Array.prototype.forEach.call(svgEl.querySelectorAll('.drag-dot'), function(dot){
        dot.addEventListener('pointerdown', function(e){
          e.preventDefault(); e.stopPropagation();
          dragging = true;
          tooltip.hidden = true;
          wrap.classList.add('dragging');
          var ch = svgEl.querySelector('.crosshair'); if (ch) ch.remove();
          var m = dot.getAttribute('data-m');
          var halo = dot.previousElementSibling;
          var read = document.createElement('div');
          read.className = 'drag-readout';
          wrap.appendChild(read);
          try { dot.setPointerCapture(e.pointerId); } catch(_e){}
          function valAt(ev){
            var rect = svgEl.getBoundingClientRect();
            var yUser = ((ev.clientY-rect.top)/rect.height)*H;
            var v = (padT + plotH - yUser)/plotH * niceMax;
            return Math.max(0, Math.round(v));
          }
          function move(ev){
            var v = valAt(ev), y = yAt(v).toFixed(2);
            dot.setAttribute('cy', y);
            if (halo && halo.classList.contains('drag-halo')) halo.setAttribute('cy', y);
            read.textContent = m + '  ' + fmt(v) + '개';
            opts.onDrag(m, v, false);
          }
          function up(ev){
            svgEl.removeEventListener('pointermove', move);
            svgEl.removeEventListener('pointerup', up);
            svgEl.removeEventListener('pointercancel', up);
            dragging = false;
            wrap.classList.remove('dragging');
            if (read.parentNode) read.remove();
            opts.onDrag(m, valAt(ev), true);
          }
          var ds = series.filter(function(x){ return x.draggable; })[0];
          read.textContent = m + '  ' + fmt(ds ? ds.values[m] : 0) + '개';
          svgEl.addEventListener('pointermove', move);
          svgEl.addEventListener('pointerup', up);
          svgEl.addEventListener('pointercancel', up);
        });
      });
    }

    function handleMove(e){
      if (dragging) return;
      var rect = svgEl.getBoundingClientRect();
      var frac = Math.min(1, Math.max(0, (e.clientX-rect.left)/rect.width));
      var xUser = frac*W;
      var idx = months.length<=1 ? 0 : Math.round(((xUser-padL)/plotW)*(months.length-1));
      idx = Math.min(months.length-1, Math.max(0, idx));
      var m = months[idx];
      var old = svgEl.querySelector('.crosshair'); if (old) old.remove();
      var cx = xAt(idx);
      var line = document.createElementNS('http://www.w3.org/2000/svg','line');
      line.setAttribute('x1',cx); line.setAttribute('x2',cx);
      line.setAttribute('y1',padT); line.setAttribute('y2',padT+plotH);
      line.setAttribute('class','crosshair');
      svgEl.appendChild(line);

      var rows = series.map(function(s){ return {name:s.name,color:s.color,v:s.values[m]}; }).filter(function(r){ return r.v!=null; });
      tooltip.innerHTML='';
      var title = document.createElement('div'); title.className='tooltip-title'; title.textContent=m;
      tooltip.appendChild(title);

      function addSec(text){
        var d=document.createElement('div'); d.className='tooltip-sec'; d.textContent=text;
        tooltip.appendChild(d);
      }
      function addRow(color, nm, v, unit, dim){
        var r=document.createElement('div'); r.className='tooltip-row'+(dim?' dim':'');
        var k=document.createElement('span'); k.className='tooltip-key';
        if (color){ k.style.background=color; if(dim) k.style.opacity='0.55'; } else { k.style.background='transparent'; }
        var n2=document.createElement('span'); n2.className='tooltip-name'; n2.textContent=nm;
        var vv=document.createElement('span'); vv.className='tooltip-value'; vv.textContent=fmt(v)+(unit||'');
        r.appendChild(k); r.appendChild(n2); r.appendChild(vv); tooltip.appendChild(r);
        return r;
      }

      var bb = (bars && bars.map[m]) ? bars.map[m] : null;

      // ── 출고 ──
      if (rows.length){
        addSec('출고');
        rows.forEach(function(r){
          var rr = addRow(r.color, r.name, r.v, '개');
          // 그 달 홀딩이 있으면 날짜까지 같이 보여준다.
          // 뺀 것과 안 뺀 것을 구분해서, 왜 이 숫자가 나왔는지 화면에서 바로 확인되게.
          var hd = holdOf(opts.product, m);
          if (hd && r.color === 'var(--series-1)'){
            var n2 = rr.querySelector('.tooltip-name');
            var s = document.createElement('span'); s.className='tt-hold';
            s.textContent = ' (홀딩 '+holdText(hd)+
              (hd.cut ? ' · '+fmt(hd.cut)+'개 뺌' : ' · 이번 달 아니라 안 뺌')+')';
            n2.appendChild(s);
          }
        });
      }

      // ── 재고 ── (출고와 섞이지 않게 나눠서 보여준다)
      if (bb){
        addSec('재고');
        if (bb.projected){
          // '예측 출고(차감)'은 위 예측선과 같은 값이라 중복이다. 빼고 재고만 보여준다.
          addRow(bb.close<0?'var(--series-5)':'var(--fc-neon)',
                 bb.close<0?'예상 재고 (부족)':'예상 재고', bb.close, '개', true);
        } else {
          addRow('var(--series-1)', bb.is_snapshot?'재고 (실사)':'재고', bb.qty, '개', true);
        }
        if (bb.inbound){
          var ir = addRow('var(--series-3)', '입고 예정', bb.inbound, '개', true);
          var det = inboundDetail(opts.product, m);
          if (det){
            var s2 = document.createElement('span'); s2.className='tt-hold';
            s2.textContent = ' ('+det+')';
            ir.querySelector('.tooltip-name').appendChild(s2);
          }
        }
      }
      tooltip.hidden = rows.length===0 && !bb;
      if (tooltip.hidden) return;
      // 커서를 따라다니되 선을 가리지 않게: 가로만 따라가고 세로는 위/아래 구석에 붙인다.
      // 오른쪽 끝에서는 왼쪽으로 넘겨 잘리지 않게 한다.
      var leftPx = e.clientX-rect.left, topPx = e.clientY-rect.top;
      var tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
      var x = leftPx + 14;
      if (x + tw > rect.width - 4) x = leftPx - tw - 14;
      tooltip.style.left = Math.max(4, Math.min(rect.width-tw-4, x))+'px';
      // 커서가 위쪽에 있으면 툴팁은 아래로, 아래쪽이면 위로 — 항상 커서 반대편
      var y = (topPx < rect.height/2) ? (rect.height - th - 8) : 8;
      tooltip.style.top = Math.max(4, y)+'px';
    }
    svgEl.addEventListener('pointermove', handleMove);
    svgEl.addEventListener('pointerleave', function(){
      tooltip.hidden=true;
      var old = svgEl.querySelector('.crosshair'); if (old) old.remove();
    });

    if ((series.length>=2 || bars) && opts.legend!==false){
      var legend = document.createElement('div'); legend.className='legend';
      series.forEach(function(s){
        var item = document.createElement('span'); item.className='legend-item';
        var sw = document.createElement('span'); sw.className='legend-swatch';
        if (s.dashed){ sw.style.backgroundImage = 'linear-gradient(90deg, '+s.color+' 50%, transparent 50%)'; sw.style.backgroundSize='6px 2px'; }
        else { sw.style.background = s.color; }
        var label = document.createElement('span'); label.textContent = s.name;
        item.appendChild(sw); item.appendChild(label);
        legend.appendChild(item);
      });
      if (bars){
        [{c:'var(--series-1)',op:0.16,t:'재고 (막대·오른쪽축)'},
         {c:'var(--fc-neon)',op:0.22,t:'예상 재고 (예측 출고 차감)'},
         {c:'var(--series-3)',op:0.28,t:'추가발주 입고분'}].forEach(function(it){
          var item = document.createElement('span'); item.className='legend-item';
          var sw = document.createElement('span'); sw.className='legend-swatch'; sw.style.background=it.c; sw.style.opacity=it.op;
          var label = document.createElement('span'); label.textContent=it.t;
          item.appendChild(sw); item.appendChild(label); legend.appendChild(item);
        });
      }
      wrap.appendChild(legend);
    }
  }

  function renderSparkline(container, valuesObj, months, color){
    var W=180,H=40,pad=3;
    var pts=[];
    months.forEach(function(m,i){ var v = valuesObj[m]; if (v!=null) pts.push([i,v]); });
    if (!pts.length){ container.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" class="spark-svg"></svg>'; return; }
    var maxV = Math.max.apply(null, pts.map(function(p){return p[1];}).concat([1]));
    var n = months.length;
    var path = pts.map(function(p,idx){
      var x = pad + (n<=1?0:(p[0]/(n-1))*(W-2*pad));
      var y = H-pad-(p[1]/maxV)*(H-2*pad);
      return (idx===0?'M':'L')+x.toFixed(1)+','+y.toFixed(1);
    }).join(' ');
    var last = pts[pts.length-1];
    var lastX = pad + (n<=1?0:(last[0]/(n-1))*(W-2*pad));
    var lastY = H-pad-(last[1]/maxV)*(H-2*pad);
    container.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" class="spark-svg" preserveAspectRatio="none"><path d="'+path+'" fill="none" stroke="'+color+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="'+lastX+'" cy="'+lastY+'" r="3" fill="'+color+'"/></svg>';
  }


  var brandChart3D = true;   // 입체 보기 기본 ON

  function brandSeries(){
    var visibleBrands = brandOrder.filter(function(b){ return activeBrands[b]; });
    return visibleBrands.map(function(b){
      var idx = brandOrder.indexOf(b);
      var prods = products.filter(function(p){ return brandOf(p)===b; });
      var values = {};
      HIST_MONTHS.forEach(function(m){
        var sum=null;
        prods.forEach(function(p){ var v=history[p][m]; if (v!=null) sum=(sum||0)+v; });
        if (sum!=null) values[m]=sum;
      });
      return {name:b, color:colorOfBrand(b, idx), values:values};
    });
  }

  function renderBrandChart(){
    var host = document.getElementById('brandChart');
    var series = brandSeries();
    if (brandChart3D){
      render3DBrandChart(host, series, HIST_MONTHS);
      return;
    }
    // 평면 모드 — 기존 선그래프 + 입체로 돌아가는 버튼
    host.innerHTML = '<div class="chart3d-wrap">'+
      '<button type="button" class="chart3d-toggle" id="brand3dToggle">입체로 보기</button>'+
      '<div id="brandChartFlat"></div></div>';
    renderLineChart(document.getElementById('brandChartFlat'), series, HIST_MONTHS, {height:280});
    host.querySelector('#brand3dToggle').addEventListener('click', function(){
      brandChart3D = true; renderBrandChart();
    });
  }

  // ── 입체(의사 3D) 리본 차트 ──
  // 브랜드마다 뒤쪽으로 밀린 평면에 그려서 깊이를 만들고,
  // 각 리본에 윗면(extrude)을 붙여 두께가 있는 것처럼 보이게 한다.
  function render3DBrandChart(container, series, months){
    var hasData = months.length && series.some(function(s){ return Object.keys(s.values).length; });
    if (!hasData){ container.innerHTML = '<div class="empty-note">표시할 데이터가 없어요</div>'; return; }

    var n = series.length;
    var W = 900, H = 440;
    var padL = 58, padR = 22, padT = 34, padB = 48;
    var dxL = 26, dyL = 26;                 // 층(브랜드)당 깊이 이동 — 층이 확실히 분리돼 보이게
    var ext = { x: 16, y: -11 };            // 리본 두께(윗면 돌출)
    var depthX = (n-1)*dxL, depthY = (n-1)*dyL;
    var plotW = W - padL - padR - depthX - ext.x;
    var plotH = H - padT - padB - depthY;

    var allVals = [];
    series.forEach(function(s){ months.forEach(function(m){ if (s.values[m]!=null) allVals.push(s.values[m]); }); });
    var niceMax = niceCeil(Math.max.apply(null, allVals) || 1);

    function xAt(i){ return months.length<=1 ? 0 : (i/(months.length-1))*plotW; }
    function yAt(v){ return plotH - (Math.max(0,v)/niceMax)*plotH; }

    var svg = '<svg viewBox="0 0 '+W+' '+H+'" class="chart-svg chart3d" preserveAspectRatio="xMidYMid meet">';

    // 그라디언트(면마다 밝기를 달리해 입체감)
    svg += '<defs>';
    series.forEach(function(s,j){
      svg += '<linearGradient id="g3d'+j+'" x1="0" y1="0" x2="0" y2="1">'+
             '<stop offset="0%" stop-color="'+s.color+'" stop-opacity="0.9"/>'+
             '<stop offset="100%" stop-color="'+s.color+'" stop-opacity="0.42"/>'+
             '</linearGradient>';
    });
    svg += '</defs>';

    // 바닥 격자 (원근에 맞춰 기울인 평면)
    var fx0 = padL, fy0 = padT + depthY;                    // 맨 앞 층 원점
    function P(i, v, d){  // d = 깊이 층 인덱스
      return [ fx0 + xAt(i) + d*dxL, fy0 + yAt(v) - d*dyL ];
    }
    var steps = 4;
    for (var s0=0; s0<=steps; s0++){
      var gv = niceMax*s0/steps;
      var a = P(0, gv, 0), b = P(months.length-1, gv, 0), c = P(months.length-1, gv, n-1);
      svg += '<path d="M'+a[0].toFixed(1)+','+a[1].toFixed(1)+' L'+b[0].toFixed(1)+','+b[1].toFixed(1)+
             ' L'+c[0].toFixed(1)+','+c[1].toFixed(1)+'" class="grid3d"/>';
      svg += '<text x="'+(a[0]-9)+'" y="'+(a[1]+3.5)+'" class="axis-label" text-anchor="end">'+fmtCompact(gv)+'</text>';
    }
    // 깊이 방향 모서리
    var o0=P(0,0,0), o1=P(0,0,n-1), e0=P(months.length-1,0,0), e1=P(months.length-1,0,n-1);
    svg += '<path d="M'+o0[0].toFixed(1)+','+o0[1].toFixed(1)+' L'+o1[0].toFixed(1)+','+o1[1].toFixed(1)+'" class="grid3d"/>';
    svg += '<path d="M'+e0[0].toFixed(1)+','+e0[1].toFixed(1)+' L'+e1[0].toFixed(1)+','+e1[1].toFixed(1)+'" class="grid3d"/>';

    // 뒤쪽 층부터 그려서 앞 층이 덮도록
    for (var j=n-1; j>=0; j--){
      var s = series[j], d = j;
      var idx = [];
      for (var i=0;i<months.length;i++) if (s.values[months[i]]!=null) idx.push(i);
      if (!idx.length) continue;

      var front = idx.map(function(i){ return P(i, s.values[months[i]], d); });
      var back  = front.map(function(p){ return [p[0]+ext.x, p[1]+ext.y]; });
      function poly(pts){ return pts.map(function(p,k){ return (k?'L':'M')+p[0].toFixed(1)+','+p[1].toFixed(1); }).join(' '); }

      var baseA = P(idx[0], 0, d), baseB = P(idx[idx.length-1], 0, d);
      // 앞면 (곡선 아래 채움)
      svg += '<path class="rib-face" data-j="'+j+'" d="'+poly(front)+
             ' L'+baseB[0].toFixed(1)+','+baseB[1].toFixed(1)+
             ' L'+baseA[0].toFixed(1)+','+baseA[1].toFixed(1)+' Z" fill="url(#g3d'+j+')"/>';
      // 윗면 (두께)
      svg += '<path class="rib-top" data-j="'+j+'" d="'+poly(front)+' '+
             poly(back.slice().reverse()).replace(/^M/,'L')+' Z" fill="'+s.color+'" opacity="0.9"/>';
      // 앞 능선
      svg += '<path class="rib-edge" data-j="'+j+'" d="'+poly(front)+'" fill="none" stroke="'+s.color+'" stroke-width="2"/>';
    }

    // 월 라벨 (맨 앞 층 기준, 촘촘하면 건너뛰기)
    var every = Math.ceil(months.length/12);
    months.forEach(function(m,i){
      if (i%every) return;
      var p = P(i, 0, 0);
      svg += '<text x="'+p[0].toFixed(1)+'" y="'+(p[1]+17)+'" class="axis-label" text-anchor="middle">'+m.slice(2)+'</text>';
    });

    svg += '</svg>';

    var legend = '<div class="chart3d-legend">' + series.map(function(s,j){
      var tot = months.reduce(function(a,m){ return a + (s.values[m]||0); }, 0);
      return '<span class="l3d" data-j="'+j+'"><i style="background:'+s.color+'"></i>'+
             escapeXML(s.name)+' <b>'+fmt(tot)+'</b></span>';
    }).join('') + '</div>';

    var toggle = '<button type="button" class="chart3d-toggle" id="brand3dToggle">'+
                 (brandChart3D?'평면으로 보기':'입체로 보기')+'</button>';

    container.innerHTML = '<div class="chart3d-wrap">'+toggle+legend+svg+'</div>';

    // 범례/리본 호버 → 해당 브랜드만 강조
    var wrap = container.querySelector('.chart3d-wrap');
    function focus(j){
      wrap.classList.add('focusing');
      Array.prototype.forEach.call(wrap.querySelectorAll('[data-j]'), function(el){
        el.classList.toggle('on', el.getAttribute('data-j')===String(j));
      });
    }
    function unfocus(){
      wrap.classList.remove('focusing');
      Array.prototype.forEach.call(wrap.querySelectorAll('[data-j]'), function(el){ el.classList.remove('on'); });
    }
    Array.prototype.forEach.call(wrap.querySelectorAll('.l3d, .rib-face, .rib-top'), function(el){
      el.addEventListener('mouseenter', function(){ focus(el.getAttribute('data-j')); });
      el.addEventListener('mouseleave', unfocus);
    });
    wrap.querySelector('#brand3dToggle').addEventListener('click', function(){
      brandChart3D = !brandChart3D;
      renderBrandChart();
    });
  }


  // ── 브랜드별 출고 비중 원그래프 ──
  var pieMonth = null;        // 기준월 (기본: 실적이 있는 가장 최근 달 = 지난달)
  var pieMonthPinned = false; // 사장님이 직접 고른 달이면 true — 그 전엔 달력 따라 자동으로 넘어감

  function monthsWithData(){
    return HIST_MONTHS.filter(function(m){
      return products.some(function(p){ return history[p] && history[p][m]!=null; });
    });
  }

  function prevYM(){
    // 지난달 (이번달이 2026-07 이면 2026-06)
    var d = new Date(); d.setDate(1); d.setMonth(d.getMonth()-1);
    var mm = d.getMonth()+1;
    return d.getFullYear()+'-'+(mm<10?'0':'')+mm;
  }

  function defaultPieMonth(avail){
    // 기본은 '지난달'. 지난달 실적이 없으면 그보다 앞선 달 중 가장 최근.
    // (미래 날짜로 잘못 들어간 값이 기본으로 잡히지 않게 함)
    var prev = prevYM();
    if (avail.indexOf(prev) >= 0) return prev;
    var past = avail.filter(function(m){ return m <= prev; });
    return past.length ? past[past.length-1] : avail[avail.length-1];
  }

  function renderBrandPie(){
    var container = document.getElementById('brandPie');
    var sel = document.getElementById('pieMonth');
    var monthLabel = document.getElementById('pieMonthLabel');
    var avail = monthsWithData();
    if (!avail.length){ container.innerHTML = '<div class="empty-note">표시할 실적이 없어요</div>'; return; }

    // 기준월 기본값: 지난달 (이번달이 7월이면 6월).
    // 직접 고르지 않았으면 매번 달력에서 다시 계산 → 매달 1일에 자동으로 다음 달로 넘어간다.
    if (!pieMonthPinned || !pieMonth || avail.indexOf(pieMonth)<0) pieMonth = defaultPieMonth(avail);

    // 월 선택 드롭다운 (최근이 위로)
    sel.innerHTML = avail.slice().reverse().map(function(m){
      var y=m.slice(0,4), mo=parseInt(m.slice(5,7),10);
      return '<option value="'+m+'"'+(m===pieMonth?' selected':'')+'>'+y+'년 '+mo+'월</option>';
    }).join('');

    // 출고 비중은 항상 전체 브랜드 기준 (위 브랜드 필터와 무관하게 고정)
    var visibleBrands = brandOrder.slice();

    // 브랜드별 집계 + SKU 내역
    var slices = [];
    visibleBrands.forEach(function(b){
      var skus = [];
      products.filter(function(p){ return brandOf(p)===b; }).forEach(function(p){
        var v = history[p] ? history[p][pieMonth] : null;
        if (v!=null && v>0) skus.push({name:p, qty:v});
      });
      var total = skus.reduce(function(a,c){ return a+c.qty; }, 0);
      if (total>0){
        skus.sort(function(x,y){ return y.qty-x.qty; });
        slices.push({brand:b, total:total, skus:skus, color:colorOfBrand(b, brandOrder.indexOf(b))});
      }
    });

    var grand = slices.reduce(function(a,c){ return a+c.total; }, 0);
    var ymLabel = pieMonth.slice(0,4)+'년 '+parseInt(pieMonth.slice(5,7),10)+'월';
    monthLabel.textContent = '· '+ymLabel+' 기준';

    if (!grand){
      container.innerHTML = '<div class="empty-note">'+ymLabel+'에는 출고 실적이 없어요</div>';
      return;
    }

    // ── SVG 도넛 그리기 ──
    var SZ=250, R=100, IR=58, CX=SZ/2, CY=SZ/2;
    function pt(cx,cy,r,deg){
      var a=(deg-90)*Math.PI/180;
      return [cx+r*Math.cos(a), cy+r*Math.sin(a)];
    }
    function arcPath(startDeg,endDeg){
      // 100% 한 조각이면 원이 안 그려지므로 살짝 줄임
      if (endDeg-startDeg >= 360) endDeg = startDeg + 359.99;
      var large = (endDeg-startDeg)>180 ? 1 : 0;
      var o1=pt(CX,CY,R,startDeg), o2=pt(CX,CY,R,endDeg);
      var i2=pt(CX,CY,IR,endDeg), i1=pt(CX,CY,IR,startDeg);
      return 'M'+o1[0].toFixed(2)+','+o1[1].toFixed(2)+
             ' A'+R+','+R+' 0 '+large+' 1 '+o2[0].toFixed(2)+','+o2[1].toFixed(2)+
             ' L'+i2[0].toFixed(2)+','+i2[1].toFixed(2)+
             ' A'+IR+','+IR+' 0 '+large+' 0 '+i1[0].toFixed(2)+','+i1[1].toFixed(2)+' Z';
    }

    var acc=0, paths='';
    slices.forEach(function(s,i){
      var deg = s.total/grand*360;
      paths += '<path class="pie-slice" data-i="'+i+'" d="'+arcPath(acc, acc+deg)+'" fill="'+s.color+'"></path>';
      acc += deg;
    });

    var svg = '<svg viewBox="0 0 '+SZ+' '+SZ+'" width="'+SZ+'" height="'+SZ+'" role="img" aria-label="'+escapeXML(ymLabel)+' 브랜드별 출고 비중">'+
      paths+
      '<text class="pie-center-label" x="'+CX+'" y="'+(CY-8)+'" text-anchor="middle">'+escapeXML(ymLabel)+' 합계</text>'+
      '<text class="pie-center-value" x="'+CX+'" y="'+(CY+14)+'" text-anchor="middle">'+fmt(grand)+'개</text>'+
      '</svg>';

    var legend = '<div class="pie-legend">' + slices.map(function(s,i){
      var pct = s.total/grand*100;
      return '<div class="pie-legend-row" data-i="'+i+'">'+
        '<span class="sw" style="background:'+s.color+'"></span>'+
        '<span class="nm">'+escapeXML(s.brand)+'</span>'+
        '<span class="qt">'+fmt(s.total)+'</span>'+
        '<span class="pc">'+pct.toFixed(1)+'%</span></div>';
    }).join('') + '</div>';

    container.innerHTML = '<div class="pie-wrap">'+svg+legend+'</div>';

    // ── 호버: 브랜드 SKU별 수량·비중 ──
    var wrap = container.querySelector('.pie-wrap');
    var tip = null;
    function showTip(i, ev){
      var s = slices[i];
      var rows = s.skus.map(function(k){
        return '<tr><td class="n">'+escapeXML(shortName(k.name))+'</td>'+
               '<td class="v">'+fmt(k.qty)+'</td>'+
               '<td class="s">'+(k.qty/s.total*100).toFixed(1)+'%</td></tr>';
      }).join('');
      if (!tip){ tip = document.createElement('div'); tip.className='pie-tip'; wrap.appendChild(tip); }
      tip.innerHTML =
        '<h4><span class="sw" style="background:'+s.color+'"></span>'+escapeXML(s.brand)+'</h4>'+
        '<table>'+rows+'</table>'+
        '<div class="tot"><span>브랜드 합계</span><b>'+fmt(s.total)+'개 · '+(s.total/grand*100).toFixed(1)+'%</b></div>';
      wrap.classList.add('hovering');
      Array.prototype.forEach.call(wrap.querySelectorAll('.pie-slice'), function(el){
        el.classList.toggle('on', el.getAttribute('data-i')===String(i));
      });
      moveTip(ev);
    }
    function moveTip(ev){
      if (!tip) return;
      var r = wrap.getBoundingClientRect();
      var x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
      // 오른쪽/아래로 넘치면 반대편에 붙임
      if (x + tip.offsetWidth > r.width) x = Math.max(0, ev.clientX - r.left - tip.offsetWidth - 14);
      if (y + tip.offsetHeight > r.height) y = Math.max(0, r.height - tip.offsetHeight);
      tip.style.left = x+'px'; tip.style.top = y+'px';
    }
    function hideTip(){
      if (tip){ tip.remove(); tip=null; }
      wrap.classList.remove('hovering');
      Array.prototype.forEach.call(wrap.querySelectorAll('.pie-slice'), function(el){ el.classList.remove('on'); });
    }
    Array.prototype.forEach.call(wrap.querySelectorAll('.pie-slice, .pie-legend-row'), function(el){
      var i = +el.getAttribute('data-i');
      el.addEventListener('mouseenter', function(ev){ showTip(i, ev); });
      el.addEventListener('mousemove', moveTip);
      el.addEventListener('mouseleave', hideTip);
    });
  }

  function shortName(p){
    // '보르헤스_올리브유500' → '올리브유500' (브랜드 접두어는 이미 제목에 있음)
    var i = p.indexOf('_');
    return i>0 ? p.slice(i+1) : p;
  }

  // ═══════════════ 신상품관리 ═══════════════
  var npSelected = null;
  var npBrand = '전체';

  function npMonths(p){ return Object.keys(history[p]||{}).sort(); }

  function npStats(p){
    var ms = npMonths(p);
    if (!ms.length) return null;
    var vals = ms.map(function(m){ return history[p][m]; });
    var total = vals.reduce(function(a,c){ return a+c; }, 0);
    var last = vals[vals.length-1];
    var prev = vals.length>1 ? vals[vals.length-2] : null;
    return {
      months: ms, vals: vals, total: total,
      launch: ms[0], lastMonth: ms[ms.length-1], last: last, prev: prev,
      delta: (prev!=null && prev>0) ? (last-prev)/prev*100 : null,
      avg: total/ms.length,
      peak: Math.max.apply(null, vals)
    };
  }

  // ── 신상품: 하루하루 얼마나 나갔나 ──────────────────────────────
  // 재고표를 줄 때마다 쌓인 '그날까지 누적'에서 하루치 차이를 뽑아 보여준다.
  function renderNewDaily(){
    var el = document.getElementById('npDaily');
    var sub = document.getElementById('npDailySub');
    if (!el) return;

    // 신상품별로 (날짜 → 누적) 을 모으고, 앞뒤 차이로 하루치를 만든다
    var cols = {};          // 'MM/DD' → true
    var dayYm = {};         // 'MM/DD' → 'YYYY-MM' (달별로 접을 때 쓴다)
    var rows = [];
    newProducts.forEach(function(p){
      var byMonth = (dailyData||{})[p] || {};
      var pts = [];
      Object.keys(byMonth).sort().forEach(function(m){
        Object.keys(byMonth[m]).map(Number).sort(function(a,b){return a-b;}).forEach(function(d){
          pts.push({ym:m, d:d, key:m.slice(5)+'/'+(d<10?'0':'')+d,
                    cum:(byMonth[m][String(d)]||{}).real || 0});
        });
      });
      if (!pts.length) return;
      var per = {};
      pts.forEach(function(pt, i){
        // 달이 바뀌면 누적이 0부터 다시 시작하므로 그 날은 누적값 자체가 하루치
        var prev = (i>0 && pts[i-1].ym===pt.ym) ? pts[i-1].cum : 0;
        per[pt.key] = pt.cum - prev;
        cols[pt.key] = true;
        dayYm[pt.key] = pt.ym;
      });
      rows.push({p:p, per:per, last:pts[pts.length-1]});
    });

    var days = Object.keys(cols).sort();
    if (!rows.length || !days.length){
      sub.textContent = '';
      el.innerHTML = '<div class="attn-none" style="color:var(--text-muted)">'+
        '아직 쌓인 기록이 없어요. 수요예측 탭에서 ERP 재고표를 올리면 그날치가 쌓입니다.</div>';
      return;
    }

    // ── 달별로 나눈다 ─────────────────────────────────────────────
    // 하루치를 두 달치 다 늘어놓으면 열이 60개가 넘어 표를 옆으로 한참 밀어야 한다.
    // 지나간 달은 '합계' 한 칸으로 접고, 이번 달만 하루하루 펼쳐 둔다.
    // 접힌 달 머리글을 누르면 그 달도 펼쳐진다.
    var byYm = {};                       // '2026-08' → ['08/12', ...]
    days.forEach(function(k){
      var ym = dayYm[k];
      if (!ym) return;
      (byYm[ym] = byYm[ym] || []).push(k);
    });
    var yms = Object.keys(byYm).sort();
    var lastYm = yms[yms.length - 1];

    var colDefs = [];                    // {type:'day'|'month', ym, key, label}
    yms.forEach(function(ym){
      var open = (ym === lastYm) || npOpenMonths[ym];
      if (open){
        byYm[ym].forEach(function(k, i){
          // 펼친 지난 달은 첫 칸 머리글을 누르면 다시 접힌다 (이번 달은 항상 펼침)
          colDefs.push({type:'day', ym:ym, key:k, label:k,
                        collapse: (i === 0 && ym !== lastYm)});
        });
      } else {
        colDefs.push({type:'month', ym:ym, keys:byYm[ym], label:(+ym.slice(5)) + '월 합계'});
      }
    });
    function cellOf(r, c){
      if (c.type === 'day') return r.per[c.key];
      var any = false, s = 0;
      c.keys.forEach(function(k){ if (r.per[k] != null){ any = true; s += r.per[k]; } });
      return any ? s : null;
    }

    sub.textContent = days.length===1
      ? '재고표 1장뿐이라 첫날 누적만 있어요. 내일 것부터 하루치가 보입니다.'
      : (days[0]+' ~ '+days[days.length-1]+' · 지나간 달은 합계로 접었어요 (머리글을 누르면 펼쳐져요)');

    // 하루치가 큰 상품부터
    rows.sort(function(a,b){
      var la=a.per[days[days.length-1]]||0, lb=b.per[days[days.length-1]]||0;
      return lb-la;
    });

    var head = '<tr><th>상품</th>'+colDefs.map(function(c){
      if (c.type==='month')
        return '<th class="np-mcol" data-ym="'+c.ym+'" title="누르면 하루하루 펼쳐져요">▸ '+c.label+'</th>';
      if (c.collapse)
        return '<th class="np-mcol" data-ym="'+c.ym+'" title="누르면 다시 합계로 접혀요">◂ '+c.label+'</th>';
      return '<th>'+c.label+'</th>';
    }).join('')+'<th>합계</th><th>재고</th></tr>';
    var body = rows.map(function(r){
      var tot = days.reduce(function(a,d){ return a + (r.per[d]||0); }, 0);
      var tds = colDefs.map(function(c){
        var v = cellOf(r, c);
        if (v==null) return '<td class="num np-x">·</td>';
        var cls = v>0 ? 'np-pos' : (v<0 ? 'np-neg' : 'np-zero');
        return '<td class="num '+cls+(c.type==='month'?' np-mcell':'')+'">'+(v===0?'0':fmt(v))+'</td>';
      }).join('');
      var stock = (inventoryRaw[r.p]||{}).stock || {};
      var st = stock[r.last.ym];
      return '<tr data-p="'+escapeXML(r.p)+'"><td class="np-nm">'+escapeXML(r.p)+'</td>'+tds+
             '<td class="num"><b>'+fmt(tot)+'</b></td>'+
             '<td class="num np-stock">'+(st==null?'-':fmt(st))+'</td></tr>';
    }).join('');

    var totals = colDefs.map(function(c){
      var s = rows.reduce(function(a,r){ return a + (cellOf(r, c)||0); }, 0);
      return '<td class="num'+(c.type==='month'?' np-mcell':'')+'"><b>'+fmt(s)+'</b></td>';
    }).join('');
    var grand = rows.reduce(function(a,r){
      return a + days.reduce(function(x,d){ return x + (r.per[d]||0); }, 0); }, 0);

    el.innerHTML = '<div class="np-daily-wrap"><table class="np-daily">'+
      '<thead>'+head+'</thead><tbody>'+body+'</tbody>'+
      '<tfoot><tr><td>합계</td>'+totals+'<td class="num"><b>'+fmt(grand)+'</b></td><td></td></tr></tfoot>'+
      '</table></div>';

    Array.prototype.forEach.call(el.querySelectorAll('.np-mcol'), function(th){
      th.addEventListener('click', function(){
        var ym = th.getAttribute('data-ym');
        npOpenMonths[ym] = !npOpenMonths[ym];
        renderNewDaily();
      });
    });

    Array.prototype.forEach.call(el.querySelectorAll('tbody tr'), function(tr){
      tr.addEventListener('click', function(){
        var p = tr.getAttribute('data-p');
        if (npSelect) npSelect(p);
      });
    });
  }
  var npSelect = null;
  var npOpenMonths = {};   // 접어 둔 지난 달 중 사용자가 펼친 것

  function renderNewProducts(){
    var grid = document.getElementById('npGrid');
    var kpi = document.getElementById('npKpiRow');
    var cnt = document.getElementById('npCount');
    if (!grid) return;

    var tabsEl = document.getElementById('npTabs');
    if (!newProducts.length){
      cnt.textContent = '';
      kpi.innerHTML = '';
      if (tabsEl) tabsEl.innerHTML = '';
      grid.innerHTML = '<div class="empty-note">등록된 신상품이 없어요. 위 <b>➕ 새 제품 추가</b> 버튼으로 등록해보세요.</div>';
      document.getElementById('npChart').innerHTML = '';
      document.getElementById('npDetail').innerHTML = '';
      return;
    }

    renderNewDaily();

    var stats = {};
    newProducts.forEach(function(p){ stats[p] = npStats(p); });

    // ── 브랜드 탭 ──
    var brands = [];
    newProducts.forEach(function(p){ var b=brandOf(p); if(brands.indexOf(b)<0) brands.push(b); });
    brands.sort();
    if (npBrand!=='전체' && brands.indexOf(npBrand)<0) npBrand = '전체';
    if (tabsEl){
      tabsEl.innerHTML = ['전체'].concat(brands).map(function(b){
        var n = b==='전체' ? newProducts.length
                           : newProducts.filter(function(p){ return brandOf(p)===b; }).length;
        return '<button type="button" class="np-tab'+(b===npBrand?' on':'')+'" data-b="'+escapeXML(b)+'">'+
               escapeXML(b)+'<span class="npc">'+n+'</span></button>';
      }).join('');
      Array.prototype.forEach.call(tabsEl.querySelectorAll('.np-tab'), function(t){
        t.addEventListener('click', function(){ npBrand = t.getAttribute('data-b'); renderNewProducts(); });
      });
    }

    var shown = npBrand==='전체' ? newProducts.slice()
                                 : newProducts.filter(function(p){ return brandOf(p)===npBrand; });
    cnt.textContent = '(' + shown.length + '종' + (npBrand!=='전체' ? ' · '+npBrand : '') + ')';

    var withData = shown.filter(function(p){ return stats[p]; });

    // ── KPI (현재 탭 기준) ──
    var grandTotal = withData.reduce(function(a,p){ return a + stats[p].total; }, 0);
    var lastM = null;
    withData.forEach(function(p){ var s=stats[p]; if(!lastM || s.lastMonth>lastM) lastM = s.lastMonth; });
    var lastMonthTotal = withData.reduce(function(a,p){
      return a + ((history[p] && history[p][lastM]) || 0); }, 0);
    var best = withData.slice().sort(function(a,b){ return stats[b].total - stats[a].total; })[0];

    kpi.innerHTML =
      kpiCard('신상품 수', shown.length + '종', npBrand==='전체' ? '' : npBrand) +
      kpiCard('누적 출고', fmt(grandTotal)+'개', '출시 이후 전체') +
      kpiCard((lastM||'-')+' 출고', fmt(lastMonthTotal)+'개', '') +
      kpiCard('최다 판매', best?shortName(best):'-', best?fmt(stats[best].total)+'개':'실적 없음');

    // ── 월별 추이 (상품별 색상 누적 막대) ──
    renderNpChart(stats, withData);

    // ── 카드 목록 ──
    if (!npSelected || shown.indexOf(npSelected)<0) npSelected = best || shown[0];
    grid.innerHTML = shown.map(function(p){
      var s = stats[p];
      var cost = meta[p] && meta[p].cost;
      var costHTML = cost ? '<div class="np-cost">수입원가 '+fmt(cost)+'원</div>' : '';
      if (!s){
        // 방금 등록해서 아직 실적이 없는 제품
        return '<div class="np-card'+(p===npSelected?' selected':'')+'" data-p="'+escapeXML(p)+'">'+
          '<div class="np-nm">'+escapeXML(shortName(p))+'<span class="np-badge">NEW</span></div>'+
          '<div class="np-br">'+escapeXML(brandOf(p))+'</div>'+ costHTML +
          '<div class="np-nodata">아직 실적이 없어요 — 수요예측 탭에서 월별 값을 입력해주세요</div>'+
        '</div>';
      }
      var d = s.delta;
      var dTxt = d==null ? '—' : (d>=0?'+':'') + d.toFixed(0) + '%';
      var dCol = d==null ? 'var(--text-muted)' : (d>=0 ? 'var(--div-pos)' : 'var(--div-neg)');
      return '<div class="np-card'+(p===npSelected?' selected':'')+'" data-p="'+escapeXML(p)+'">'+
        '<div class="np-nm">'+escapeXML(shortName(p))+'<span class="np-badge">NEW</span></div>'+
        '<div class="np-br">'+escapeXML(brandOf(p))+'</div>'+ costHTML +
        '<div class="np-row"><span>출시</span><b>'+s.launch+'</b></div>'+
        '<div class="np-row"><span>누적</span><b>'+fmt(s.total)+'개</b></div>'+
        '<div class="np-row"><span>'+s.lastMonth+'</span><b>'+fmt(s.last)+'개 <span style="color:'+dCol+'">'+dTxt+'</span></b></div>'+
        '<div class="np-spark">'+sparkSVG(s.vals)+'</div>'+
      '</div>';
    }).join('');

    Array.prototype.forEach.call(grid.querySelectorAll('.np-card'), function(el){
      el.addEventListener('click', function(){
        npSelected = el.getAttribute('data-p');
        renderNewProducts();
      });
    });

    renderNpDetail(stats[npSelected], npSelected);
    renderNpEditor(npSelected);
  }

  // 신상품도 수요예측 화면과 동일하게 월별 입력 + 예측식을 쓸 수 있게 한다
  function renderNpEditor(p){
    var card = document.getElementById('npEditCard');
    if (!card) return;
    if (!p){ card.hidden = true; return; }
    card.hidden = false;
    document.getElementById('npEditTitle').textContent = shortName(p) + ' — 월별 값 입력 · 예측';

    function redraw(){
      drawForecastChart(p, document.getElementById('npFcChart'), document.getElementById('npFcNote'));
    }
    renderEditTable(document.getElementById('npEditSection'), p);
    fcPreview = null;
    renderForecastPanel(document.getElementById('npForecastSection'), p, redraw);
  }

  // ── 새 제품 등록 ──
  function npAddProduct(){
    if (!LIVE){ showToast('제품 등록은 serve 모드에서만 돼요 (python forecast_tool.py serve)', true); return; }
    // 브랜드는 기존 목록에서 고르거나 직접 입력
    var known = [];
    Object.keys(meta).forEach(function(p){
      var b = meta[p] && meta[p].brand;
      if (b && known.indexOf(b)<0) known.push(b);
    });
    known.sort();
    var m = openModal('<div class="modal"><h3>➕ 새 제품 추가</h3>'+
      '<div class="card-sub" style="margin-bottom:10px">등록하면 <b>신상품관리</b>에 바로 나타나요. '+
      '월별 실적은 수요예측 탭의 입력칸에서 넣어주세요.</div>'+
      '<div class="modal-row"><label>상품명 *</label><input id="npName" placeholder="예: 에스파뇰라_트러플오일250"></div>'+
      '<div class="modal-row"><label>브랜드 *</label>'+
        '<input id="npBrandIn" list="npBrandList" placeholder="예: 에스파뇰라">'+
        '<datalist id="npBrandList">'+known.map(function(b){ return '<option value="'+escapeXML(b)+'">'; }).join('')+'</datalist>'+
      '</div>'+
      '<div class="modal-row"><label>수입원가 (개당, 원)</label><input id="npCost" type="number" min="0" step="1" placeholder="예: 4200"></div>'+
      '<div class="modal-row"><label>상품코드</label><input id="npCode" placeholder="선택 · 예: BOIL0071"></div>'+
      '<div class="modal-row"><label>단위</label><input id="npUnit" value="개" style="max-width:90px"></div>'+
      '<div class="modal-actions"><button class="cust-btn ghost" id="npCancel">취소</button>'+
      '<button class="cust-btn" id="npOk">등록</button></div></div>');

    var nameEl = m.querySelector('#npName');
    setTimeout(function(){ nameEl.focus(); }, 30);

    function submit(){
      var name = (nameEl.value||'').trim();
      var brand = (m.querySelector('#npBrandIn').value||'').trim();
      if (!name){ showToast('상품명을 입력해주세요', true); nameEl.focus(); return; }
      if (!brand){ showToast('브랜드를 입력해주세요', true); return; }
      if (meta[name]){ showToast('이미 등록된 상품명이에요', true); nameEl.focus(); return; }
      var btn = m.querySelector('#npOk'); btn.disabled=true; btn.textContent='등록 중…';
      postJSON('/api/product', {
        product: name, brand: brand,
        cost: m.querySelector('#npCost').value,
        code: (m.querySelector('#npCode').value||'').trim(),
        unit: (m.querySelector('#npUnit').value||'개').trim(),
        "new": true
      }).then(function(newData){
        closeModal();
        npBrand = brand; npSelected = name;
        showToast('"'+shortName(name)+'" 등록 완료');
        applyData(newData);
      }).catch(function(err){
        showToast(err.message||'등록에 실패했어요', true);
        btn.disabled=false; btn.textContent='등록';
      });
    }
    m.querySelector('#npOk').addEventListener('click', submit);
    m.querySelector('#npCancel').addEventListener('click', closeModal);
    nameEl.addEventListener('keydown', function(e){ if(e.key==='Enter') submit(); });
  }

  function kpiCard(label, value, sub){
    // 기존 수요예측 KPI(kpiTile)와 같은 클래스를 써서 모양을 맞춘다
    return '<div class="kpi-tile"><div class="kpi-label">'+escapeXML(label)+'</div>'+
           '<div class="kpi-value">'+escapeXML(String(value))+'</div>'+
           (sub?'<div class="kpi-delta">'+escapeXML(sub)+'</div>':'')+'</div>';
  }

  function sparkSVG(vals){
    var W=190, H=34, mx=Math.max.apply(null, vals)||1;
    if (vals.length===1){
      return '<svg width="100%" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
        '<rect x="'+(W/2-6)+'" y="4" width="12" height="'+(H-8)+'" fill="var(--series-3)" rx="2"/></svg>';
    }
    var step = W/(vals.length-1);
    var d = vals.map(function(v,i){
      return (i?'L':'M') + (i*step).toFixed(1) + ',' + (H-4 - (v/mx)*(H-8)).toFixed(1);
    }).join(' ');
    return '<svg width="100%" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
      '<path d="'+d+'" fill="none" stroke="var(--series-3)" stroke-width="2"/></svg>';
  }

  function renderNpChart(stats, list){
    var host = document.getElementById('npChart');
    var items = list || newProducts;
    // 신상품 전체가 걸쳐 있는 월 범위
    var mset = {};
    items.forEach(function(p){ npMonths(p).forEach(function(m){ mset[m]=true; }); });
    var months = Object.keys(mset).sort();
    if (!months.length){ host.innerHTML='<div class="empty-note">아직 실적이 없어요</div>'; return; }

    var W=Math.max(560, months.length*54), H=250, PL=54, PR=14, PT=14, PB=34;
    var iw=W-PL-PR, ih=H-PT-PB;
    var totals = months.map(function(m){
      return items.reduce(function(a,p){ return a + ((history[p]&&history[p][m])||0); }, 0); });
    var mx = niceCeil(Math.max.apply(null, totals)||1);
    var bw = Math.min(38, iw/months.length*0.62);

    var g='';
    for (var t=0;t<=4;t++){
      var y=PT+ih-(t/4)*ih;
      g += '<line x1="'+PL+'" y1="'+y.toFixed(1)+'" x2="'+(W-PR)+'" y2="'+y.toFixed(1)+'" stroke="var(--gridline)"/>'+
           '<text x="'+(PL-7)+'" y="'+(y+3.5).toFixed(1)+'" text-anchor="end" font-size="10" fill="var(--text-muted)">'+fmt(mx*t/4)+'</text>';
    }

    var bars='';
    months.forEach(function(m,i){
      var cx = PL + (i+0.5)*(iw/months.length);
      var acc = 0;
      items.forEach(function(p,pi){
        var v = (history[p]&&history[p][m])||0;
        if (!v) return;
        var h = v/mx*ih, y = PT+ih-acc-h;
        acc += h;
        bars += '<rect x="'+(cx-bw/2).toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+h.toFixed(1)+
                '" fill="'+brandColor(pi)+'" opacity="0.9"><title>'+escapeXML(shortName(p))+' · '+m+' · '+fmt(v)+'개</title></rect>';
      });
      bars += '<text x="'+cx.toFixed(1)+'" y="'+(H-PB+15)+'" text-anchor="middle" font-size="9.5" fill="var(--text-muted)">'+m.slice(2)+'</text>';
    });

    var legend = '<div class="np-legend">' + items.map(function(p,pi){
      return '<span><i style="background:'+brandColor(pi)+'"></i>'+escapeXML(shortName(p))+'</span>'; }).join('') + '</div>';

    host.innerHTML = legend + '<div style="overflow-x:auto"><svg width="'+W+'" height="'+H+'">'+g+bars+'</svg></div>';
  }

  function renderNpDetail(s, p){
    var host = document.getElementById('npDetail');
    if (!s){ host.innerHTML=''; return; }
    var rows = s.months.map(function(m,i){
      var v = s.vals[i], prev = i? s.vals[i-1] : null;
      var d = (prev!=null && prev>0) ? (v-prev)/prev*100 : null;
      var share = s.total ? v/s.total*100 : 0;
      return '<tr><td>'+m+'</td><td>'+fmt(v)+'</td>'+
             '<td>'+(d==null?'—':((d>=0?'+':'')+d.toFixed(0)+'%'))+'</td>'+
             '<td>'+share.toFixed(1)+'%</td></tr>';
    }).join('');
    host.innerHTML =
      '<h3 style="font-size:13px;margin:16px 0 2px;">'+escapeXML(shortName(p))+' 월별 실적</h3>'+
      '<div class="card-sub">출시 '+s.launch+' · '+s.months.length+'개월 · 누적 '+fmt(s.total)+'개 · 월평균 '+fmt(Math.round(s.avg))+'개</div>'+
      '<table class="np-table"><thead><tr><th>월</th><th>출고량</th><th>전월비</th><th>누적비중</th></tr></thead>'+
      '<tbody>'+rows+'</tbody></table>';
  }

  function buildHistoryTableHTML(p, months){
    var html = '<table class="viz-table"><thead><tr><th>월</th><th>실적</th></tr></thead><tbody>';
    months.forEach(function(m){
      var v = history[p][m];
      if (v==null) return;
      html += '<tr><td>'+m+'</td><td>'+fmt(v)+'</td></tr>';
    });
    html += '</tbody></table>';
    return html;
  }

  function buildCompareTableHTML(rows){
    var html = '<h3 style="font-size:13px;margin:16px 0 8px;">예측 vs 실제 비교</h3>';
    html += '<table class="viz-table"><thead><tr><th>월</th><th>vintage</th><th>방식</th><th>시나리오</th><th>예측</th><th>실제</th><th>오차율</th></tr></thead><tbody>';
    rows.forEach(function(r){
      var pillCls = r.error_pct==null ? 'neutral' : (r.error_pct>=0?'pos':'neg');
      var pillText = r.error_pct==null ? '대기중' : (r.error_pct>=0?'+':'')+r.error_pct.toFixed(1)+'%';
      html += '<tr><td>'+escapeXML(r.month)+'</td><td>'+escapeXML(r.vintage)+'</td><td>'+escapeXML(r.method)+'</td><td>'+escapeXML(r.scenario)+'</td><td>'+fmt(r.forecast)+'</td><td>'+(r.actual==null?'-':fmt(r.actual))+'</td><td><span class="pill '+pillCls+'">'+pillText+'</span></td></tr>';
    });
    html += '</tbody></table>';
    return html;
  }

  function renderSparkGrid(){
    var grid = document.getElementById('sparkGrid');
    grid.innerHTML='';
    var visible = products.filter(function(p){ return activeBrands[brandOf(p)] && (!searchTerm || p.toLowerCase().indexOf(searchTerm)!==-1); });
    document.getElementById('sparkCount').textContent = '('+visible.length+'개 표시 중)';
    if (!visible.length){ grid.innerHTML = '<div class="empty-note">조건에 맞는 상품이 없어요</div>'; return; }
    visible.forEach(function(p){
      var idx = brandOrder.indexOf(brandOf(p));
      var card = document.createElement('div');
      card.className = 'spark-card'+(p===selectedProduct?' selected':'');
      card.tabIndex = 0;
      card.setAttribute('role','button');
      var brandEl = document.createElement('div'); brandEl.className='spark-brand'; brandEl.textContent = brandOf(p);
      var nameEl = document.createElement('div'); nameEl.className='spark-name'; nameEl.textContent = p; nameEl.title = p;
      var svgHolder = document.createElement('div');
      var months = HIST_MONTHS.filter(function(m){ return history[p][m]!=null; });
      var latestVal = months.length ? history[p][months[months.length-1]] : null;
      var valEl = document.createElement('div'); valEl.className='spark-value';
      valEl.textContent = months.length ? (months[months.length-1]+': '+fmt(latestVal)+'개') : '데이터 없음';
      card.appendChild(brandEl); card.appendChild(nameEl); card.appendChild(svgHolder); card.appendChild(valEl);
      grid.appendChild(card);
      renderSparkline(svgHolder, history[p], HIST_MONTHS, colorOfBrand(brandOf(p), idx));
      function select(){ selectedProduct = p; renderSparkGrid(); renderDetail(); }
      card.addEventListener('click', function(){ if(sparkDrag.moved) return; select(); });
      card.addEventListener('keydown', function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); select(); } });
    });
  }


  function renderEditTable(container, p){
    if (!LIVE){
      container.innerHTML = '<div class="edit-panel"><div class="card-sub">💡 값을 입력·정정하려면 <code>python forecast_tool.py serve</code> 로 열어주세요. (지금은 정적 스냅샷이라 저장이 안 돼요)</div></div>';
      return;
    }
    var hist = history[p] || {};
    var invP = inventoryRaw[p] || {};
    var inbounds = invP.inbound || {};
    var inbAt = invP.inbound_at || {};
    var inbLock = invP.inbound_lock || {};   // 손으로 정한 달 — 재고표가 못 덮는다
    var stocks = invP.stock || {};
    var stDay = invP.stock_day || {};

    // 연도 목록: 실적 최소연도 ~ 올해+1
    var yset = {};
    Object.keys(hist).forEach(function(m){ yset[m.slice(0,4)]=true; });
    Object.keys(inbounds).forEach(function(m){ yset[m.slice(0,4)]=true; });
    Object.keys(stocks).forEach(function(m){ yset[m.slice(0,4)]=true; });
    var nowY = parseInt(currentYM().slice(0,4),10);
    yset[nowY]=true; yset[nowY+1]=true;

    // 기본 선택월 = 마지막으로 실적을 넣은 달의 "다음 달".
    // 저장하면 applyData → 이 표가 다시 그려지면서 자동으로 또 다음 달로 넘어간다.
    var histMonths = Object.keys(hist).sort();
    var defM = histMonths.length ? numYm(ymNum(histMonths[histMonths.length-1])+1) : currentYM();
    var defY = defM.slice(0,4), defMo = parseInt(defM.slice(5,7),10);
    yset[parseInt(defY,10)] = true;      // 12월 다음달(=내년 1월)도 목록에 있게

    var years = Object.keys(yset).map(Number).sort(function(a,b){return a-b;});

    function yOpts(sel){ return years.map(function(y){ return '<option value="'+y+'"'+(String(y)===String(sel)?' selected':'')+'>'+y+'년</option>'; }).join(''); }
    function mOpts(sel){ var o=''; for(var i=1;i<=12;i++){ o+='<option value="'+i+'"'+(i===sel?' selected':'')+'>'+i+'월</option>'; } return o; }

    container.innerHTML =
      '<div class="edit-panel">'+
        '<div class="edit-title">📋 월별 값 입력·정정 — <b>마지막 입력월의 다음 달</b>이 자동으로 잡혀요. 연·월을 바꾸면 그 달 기존 값이 채워집니다. <b>수량</b>은 실적·출고 겸용(같은 값).<br>'+
        '<span style="color:var(--text-muted)">칸을 <b>비운 채로</b> 저장/정정하면 그 값이 그래프에서 사라져요. <b>0</b>은 "0개 팔림"으로 남습니다.</span></div>'+
        '<div class="compact-editor">'+
          '<select class="ce-year">'+yOpts(defY)+'</select>'+
          '<select class="ce-month">'+mOpts(defMo)+'</select>'+
          '<span class="ce-field"><label>수량 (실적·출고)</label><input class="ce-qty" type="number" min="0" placeholder="—"></span>'+
          '<span class="ce-field"><label>추가발주 (입고)</label><input class="ce-inb" type="number" min="0" placeholder="—"></span>'+
          '<span class="ce-field"><label>입고 시점</label><select class="ce-when">'+
            '<option value="early">월초</option><option value="mid" selected>중순</option>'+
            '<option value="late">월말</option></select></span>'+
          '<span class="ce-field"><label>재고 실사</label><input class="ce-stock" type="number" min="0" placeholder="—"></span>'+
          '<span class="ce-field"><label>실사한 날</label><input class="ce-sday" type="number" min="1" max="31" style="width:62px" placeholder="일"></span>'+
          '<button type="button" class="ce-save">저장/정정</button>'+
          '<button type="button" class="ce-del">삭제</button>'+
        '</div>'+
        '<div class="ce-hint card-sub"></div>'+
      '</div>';

    var yEl = container.querySelector('.ce-year');
    var mEl = container.querySelector('.ce-month');
    var qEl = container.querySelector('.ce-qty');
    var iEl = container.querySelector('.ce-inb');
    var wEl = container.querySelector('.ce-when');
    var sEl = container.querySelector('.ce-stock');
    var sdEl = container.querySelector('.ce-sday');
    var saveBtn = container.querySelector('.ce-save');
    var delBtn = container.querySelector('.ce-del');
    var hintEl = container.querySelector('.ce-hint');

    function selYM(){ var mm=parseInt(mEl.value,10); return yEl.value+'-'+(mm<10?'0':'')+mm; }
    function load(){
      var ym = selYM();
      var q = hist[ym], inb = inbounds[ym], st = stocks[ym];
      qEl.value = q!=null?q:''; iEl.value = inb!=null?inb:''; sEl.value = st!=null?st:'';
      wEl.value = (inbAt[ym] || 'mid');
      wEl.disabled = (iEl.value==='');
      // 실사한 날: 기록이 있으면 그 날, 없으면 이번 달일 때 오늘 날짜를 미리 채워둔다
      var nowYM = currentYM(), today = new Date().getDate();
      sdEl.value = (stDay[ym] != null) ? stDay[ym] : (ym===nowYM ? today : '');
      sdEl.disabled = (sEl.value==='');
      var WL = {early:'월초', mid:'중순', late:'월말'};
      var txt = ym+' 기존값 — 수량 '+(q!=null?fmt(q):'없음')+
        ' · 입고 '+(inb!=null?(fmt(inb)+'('+(WL[inbAt[ym]]||'중순')+')'):'없음')+
        ' · 재고실사 '+(st!=null?(fmt(st)+(stDay[ym]?(' ('+ym.slice(5)+'/'+stDay[ym]+'일)'):'')):'없음');
      // 손으로 정한 달이면 그걸 알려주고, 되돌릴 방법도 같이 준다
      hintEl.innerHTML = escapeXML(txt) + (inbLock[ym]
        ? ' <span class="ce-lock">🔒 입고는 직접 정한 값 — 재고표를 올려도 안 바뀝니다'+
          ' <button type="button" class="ce-unlock">재고표 값 따르기</button></span>'
        : '');
      var ub = hintEl.querySelector('.ce-unlock');
      if (ub) ub.addEventListener('click', function(){
        ub.disabled = true; ub.textContent = '푸는 중…';
        postJSON('/api/inbound/unlock', {product:p, month:ym})
          .then(function(d){ showToast(ym+' 입고 잠금을 풀었어요 — 다음 재고표부터 따라갑니다'); applyData(d); })
          .catch(function(e){ showToast(e.message||'실패했어요', true); ub.disabled=false; });
      });
      delBtn.disabled = (q==null && inb==null);
    }
    sEl.addEventListener('input', function(){
      sdEl.disabled = (sEl.value==='');
      if (sEl.value!=='' && sdEl.value===''){
        var nowYM = currentYM();
        sdEl.value = (selYM()===nowYM) ? new Date().getDate() : 1;
      }
    });
    iEl.addEventListener('input', function(){ wEl.disabled = (iEl.value===''); });
    yEl.addEventListener('change', load);
    mEl.addEventListener('change', load);
    load();

    function run(tasks, msg, btn){
      if (!tasks.length){ showToast('입력된 값이 없어요.', true); return; }
      var orig=btn.textContent; btn.disabled=true; btn.textContent='처리 중…';
      Promise.all(tasks).then(function(res){ showToast(msg); applyData(res[res.length-1]); })
        .catch(function(err){ showToast(err.message||'저장에 실패했어요', true); btn.disabled=false; btn.textContent=orig; });
    }

    saveBtn.addEventListener('click', function(){
      var ym=selYM(), tasks=[], cleared=[];
      // 값을 넣었으면 저장, 원래 값이 있었는데 칸을 비웠으면 그 항목만 삭제(그래프에서 사라짐).
      // 0 을 넣는 것과 비우는 것은 다르게 취급한다 — 0은 "0개 팔림", 빈칸은 "기록 없음".
      function field(el, cur, savePath, saveBody, label, clearKey){
        if (el.value!==''){ tasks.push(postJSON(savePath, saveBody())); }
        else if (cur!=null){
          tasks.push(postJSON('/api/clear', {product:p, month:ym, field:clearKey}));
          cleared.push(label);
        }
      }
      field(qEl, hist[ym], '/api/entry',
            function(){ return {product:p, month:ym, qty:qEl.value}; }, '수량', 'entry');
      field(iEl, inbounds[ym], '/api/inbound',
            function(){ return {product:p, month:ym, qty:iEl.value, when:wEl.value}; }, '추가발주', 'inbound');
      field(sEl, stocks[ym], '/api/stock',
            function(){ return {product:p, asof:ym, qty:sEl.value, day:sdEl.value}; }, '재고실사', 'stock');

      if (!tasks.length){ showToast('바뀐 값이 없어요.', true); return; }
      var msg = cleared.length
        ? p+' '+ym+' — '+cleared.join('·')+' 지움 (그래프에서도 제거)'
        : p+' '+ym+' 저장/정정 완료';
      run(tasks, msg, saveBtn);
    });

    delBtn.addEventListener('click', function(){
      var ym=selYM();
      run([postJSON('/api/delete', {product:p, month:ym})], p+' '+ym+' 삭제 완료 (그래프에서도 제거)', delBtn);
    });
  }

  // ── 예측 엔진 (Python ForecastEngine.forecast 를 그대로 미러링) ──
  function ymNum(ym){ var a=ym.split('-'); return parseInt(a[0],10)*12 + (parseInt(a[1],10)-1); }
  function numYm(n){ var y=Math.floor(n/12); var m=(n%12)+1; return y+'-'+(m<10?'0':'')+m; }
  function monthOf(ym){ return parseInt(ym.split('-')[1],10); }
  function meanOf(arr){ if(!arr.length) return 0; var s=0; for(var i=0;i<arr.length;i++) s+=arr[i]; return s/arr.length; }
  function seasonalIndexJS(hist){
    var vals=[]; for(var k in hist) vals.push(hist[k]);
    var idx={}; if(!vals.length){ for(var m=1;m<=12;m++) idx[m]=1.0; return idx; }
    var overall=meanOf(vals), byMonth={};
    for(var k2 in hist){ var mm=monthOf(k2); (byMonth[mm]=byMonth[mm]||[]).push(hist[k2]); }
    for(var mk in byMonth){ idx[mk]= overall ? meanOf(byMonth[mk])/overall : 1.0; }
    return idx;
  }
  function baseLevelJS(hist, window, excludeSet){
    var items=[]; for(var k in hist){ if(excludeSet[k]) continue; items.push([k,hist[k]]); }
    items.sort(function(a,b){ return a[0]<b[0]?-1:(a[0]>b[0]?1:0); });
    var recent=items.slice(Math.max(0, items.length-window));
    if(!recent.length) return 0;
    return meanOf(recent.map(function(x){ return x[1]; }));
  }
  function computeForecast(hist, cfg){
    var scenarioMap={base:1.0, optimistic:1.10, conservative:0.90};
    var mult, sc=String(cfg.scenario).trim();
    if(/^[0-9]+(\.[0-9]+)?$/.test(sc)) mult=parseFloat(sc);
    else mult = scenarioMap[sc]!=null ? scenarioMap[sc] : 1.0;
    var excludeSet={}; (cfg.exclude||[]).forEach(function(e){ if(e) excludeSet[e]=true; });
    var monthsList=[]; var s=ymNum(cfg.start);
    for(var i=0;i<cfg.months;i++) monthsList.push(numYm(s+i));
    var res={};
    if(cfg.method==='seasonal'){
      var byMonth={};
      for(var k in hist){ var m=monthOf(k); (byMonth[m]=byMonth[m]||[]).push(hist[k]); }
      monthsList.forEach(function(ym){
        var m=monthOf(ym), vs=byMonth[m];
        var val = vs ? meanOf(vs) : baseLevelJS(hist, cfg.window, excludeSet);
        res[ym]=Math.round(val*mult);
      });
    } else {
      var idx=seasonalIndexJS(hist), base=baseLevelJS(hist, cfg.window, excludeSet);
      monthsList.forEach(function(ym){
        var m=monthOf(ym);
        res[ym]=Math.round(base*(idx[m]!=null?idx[m]:1.0)*mult);
      });
    }
    return {values:res, months:monthsList};
  }

  // ── 지금 봐야 할 것 ──────────────────────────────────────────────
  // 34개를 하나씩 눌러봐야 문제를 찾던 걸, 전 품목을 한 번에 훑어서 위로 올린다.
  var ATTN_KIND = {
    stockout: {label:'재고 바닥',   cls:'bad',  rank:0},
    delay:    {label:'입고 지연 위험', cls:'bad', rank:1},
    tight:    {label:'재고 빠듯',   cls:'warn', rank:2},
    surge:    {label:'출고 급증',   cls:'warn', rank:3},
    drop:     {label:'출고 급감',   cls:'info', rank:4},
    noinb:    {label:'입고 없음',   cls:'warn', rank:5},
    odd:      {label:'값 이상',     cls:'info', rank:6}
  };
  var attnFilter = 'all';
  var attnOpen = false;      // '더 보기'로 전부 펼쳤는지
  var ATTN_SHOW = 8;         // 접혀 있을 때 보여줄 개수

  function monthsBetween(a, b){ return ymNum(b) - ymNum(a); }

  function computeAttention(){
    var out = [];
    var cm = currentYM();
    products.forEach(function(p){
      if (!activeBrands[brandOf(p)]) return;
      var st = fcStateByProduct[p] || defaultFcState(p);
      var hist = history[p] || {};
      var cfg = {method:st.method, scenario:String(st.scenario), window:st.window,
                 months:st.months, start:st.start, exclude:[]};
      var fc;
      try { fc = computeForecast(hist, cfg); } catch(e){ return; }
      var proj = computeStockProjection(p, fc);

      // 1) 재고가 언제 바닥나는지
      if (proj && proj.stockout){
        var away = monthsBetween(cm, proj.stockout);
        out.push({p:p, kind:'stockout', when:proj.stockout, away:away,
                  qty:proj.worstShort||0,
                  msg:proj.stockout+'부터 부족 · 최대 '+fmt(proj.worstShort||0)+'개 모자람'});
      } else if (proj && proj.rows && proj.rows.length){
        // 1-b) 지금은 괜찮지만, 입고가 일주일 넘게 늦으면 바닥나는지
        //      (한 칸 = 초·중·말 한 구간 ≈ 10일. 컨테이너는 통관에서 흔히 밀린다)
        var late = computeStockProjection(p, fc, 1);
        if (late && (late.stockout || late.midShort)){
          var firstInb = null;
          for (var ri = 0; ri < proj.rows.length; ri++){
            if (proj.rows[ri].inbound){ firstInb = proj.rows[ri]; break; }
          }
          if (firstInb){
            var when = late.stockout || (late.midShort || '').split('/')[0];
            var gap = late.stockout ? (late.worstShort || 0) : (late.midWorst || 0);
            out.push({p:p, kind:'delay', when:when, away:monthsBetween(cm, when), qty:gap,
                      msg:firstInb.month+' 입고('+fmt(firstInb.inbound)+'개)가 일주일만 밀려도 '+
                          when+'에 '+fmt(gap)+'개 모자라요'});
          }
        }
        // 2) 바닥은 아니지만 아슬아슬한지 (최저 재고가 월평균 출고보다 적으면 한 달치도 안 남는 것)
        var lo = proj.rows.reduce(function(a,x){ return x.close < a ? x.close : a; }, proj.rows[0].close);
        var avgOut = fc.months.length
          ? fc.months.reduce(function(a,m){ return a + (fc.values[m]||0); },0)/fc.months.length : 0;
        if (avgOut > 0 && lo < avgOut){
          out.push({p:p, kind:'tight', qty:lo,
                    msg:'가장 적을 때 '+fmt(lo)+'개 — 월평균 출고('+fmt(Math.round(avgOut))+'개)보다 적어요'});
        }
      }

      // 3) 이번 달 페이스가 지난달과 크게 다른지 (일별 기록이 있어야 판단 가능)
      var byMonth = (dailyData||{})[p] || {};
      var dm = Object.keys(byMonth).sort();
      if (dm.length){
        var lastM = dm[dm.length-1];
        var days = Object.keys(byMonth[lastM]).map(Number).sort(function(a,b){return a-b;});
        var d = days[days.length-1];
        var soFar = (byMonth[lastM][String(d)]||{}).real || 0;
        var pace = d ? soFar*(30/d) : 0;               // 이 속도면 월말에 얼마
        var prev = hist[numYm(ymNum(lastM)-1)];
        if (prev && prev > 0 && d >= 3){
          var ratio = pace / prev;
          if (ratio >= 1.5) out.push({p:p, kind:'surge', qty:Math.round(pace),
            msg:lastM+' '+d+'일까지 '+fmt(soFar)+'개 — 이 속도면 월말 '+fmt(Math.round(pace))+
                '개 (지난달 '+fmt(prev)+'개의 '+ratio.toFixed(1)+'배)'});
          else if (ratio <= 0.5) out.push({p:p, kind:'drop', qty:Math.round(pace),
            msg:lastM+' '+d+'일까지 '+fmt(soFar)+'개뿐 — 이 속도면 월말 '+fmt(Math.round(pace))+
                '개 (지난달 '+fmt(prev)+'개의 '+ratio.toFixed(1)+'배)'});
        }
      }

      // 4) 재고는 적은데 들어올 게 없는지
      var entries = inventoryData[p] || [];
      var hasInb = entries.some(function(e){ return e.inbound && e.month >= cm; });
      var latest = entries.length ? entries[entries.length-1] : null;
      if (!hasInb && latest && latest.qty != null){
        // 작업중(선물세트 조립 등) 물량은 못 팔아서, 팔 수 있는 양만 놓고 판단한다
        var usable = latest.qty - (latest.wip || 0);
        var avg2 = fc && fc.months.length
          ? fc.months.reduce(function(a,m){ return a + (fc.values[m]||0); },0)/fc.months.length : 0;
        if (avg2 > 0 && usable < avg2*2){
          out.push({p:p, kind:'noinb', qty:usable,
                    msg:'재고 '+fmt(usable)+'개인데 들어올 예정이 없어요 (월평균 '+fmt(Math.round(avg2))+'개 나감)'+
                        (latest.wip ? ' · 작업중 '+fmt(latest.wip)+'개는 뺀 값' : '')});
        }
      }

      // 5) 대놓고 이상한 값
      if (latest && latest.qty != null && latest.qty < 0){
        out.push({p:p, kind:'odd', qty:latest.qty, msg:'재고가 마이너스('+fmt(latest.qty)+'개)로 잡혀 있어요'});
      }
    });

    out.sort(function(a,b){
      var ra = ATTN_KIND[a.kind].rank, rb = ATTN_KIND[b.kind].rank;
      if (ra !== rb) return ra - rb;
      if (a.kind === 'stockout' || a.kind === 'delay') return (a.away||0) - (b.away||0);   // 임박한 것부터
      return Math.abs(b.qty||0) - Math.abs(a.qty||0);
    });
    return out;
  }

  function renderAttention(){
    var listEl = document.getElementById('attnList');
    var subEl  = document.getElementById('attnSub');
    var filEl  = document.getElementById('attnFilters');
    if (!listEl) return;

    var all = computeAttention();
    var counts = {};
    all.forEach(function(a){ counts[a.kind] = (counts[a.kind]||0)+1; });

    // 종류별 칩 (누르면 그 종류만)
    var chips = ['all'].concat(Object.keys(ATTN_KIND).filter(function(k){ return counts[k]; }));
    filEl.innerHTML = chips.map(function(k){
      var name = k==='all' ? ('전체 '+all.length) : (ATTN_KIND[k].label+' '+counts[k]);
      return '<button type="button" class="attn-chip'+(attnFilter===k?' on':'')+
             '" data-k="'+k+'">'+escapeXML(name)+'</button>';
    }).join('');
    Array.prototype.forEach.call(filEl.querySelectorAll('.attn-chip'), function(b){
      b.addEventListener('click', function(){ attnFilter = b.getAttribute('data-k'); renderAttention(); });
    });

    var shown = all.filter(function(a){ return attnFilter==='all' || a.kind===attnFilter; });

    if (!all.length){
      subEl.textContent = '';
      listEl.innerHTML = '<div class="attn-none">지금은 걸리는 곳이 없어요. 예측 기준으로 재고가 모자라는 품목이 없습니다.</div>';
      return;
    }
    var nBad = counts.stockout||0, nDelay = counts.delay||0;
    subEl.textContent = nBad
      ? (nBad+'개 품목이 재고 부족 예상이에요'+(nDelay?' · 입고가 밀리면 '+nDelay+'개 더 위험':''))
      : (nDelay ? ('입고가 일주일만 밀려도 '+nDelay+'개 품목이 바닥나요')
                : '급한 건 없지만 확인해볼 것들이에요');

    // 성수기엔 30개 넘게 걸려서 화면이 목록에 잠긴다. 급한 순서로 몇 개만 펼쳐 두고
    // 나머지는 접어 둔다 (칩으로 종류를 고르면 그 안에서 다시 센다).
    var cut = attnOpen ? shown.length : Math.min(shown.length, ATTN_SHOW);
    var rest = shown.length - cut;
    listEl.innerHTML = '<div class="attn-rows">'+shown.slice(0, cut).map(function(a){
      var k = ATTN_KIND[a.kind];
      return '<div class="attn-row" data-p="'+escapeXML(a.p)+'">'+
        '<span class="attn-tag '+k.cls+'">'+escapeXML(k.label)+'</span>'+
        '<span class="attn-name">'+escapeXML(a.p)+'</span>'+
        '<span class="attn-msg">'+escapeXML(a.msg)+'</span>'+
        '<button type="button" class="attn-go">보기</button></div>';
    }).join('')+'</div>'+
      (rest > 0 || attnOpen
        ? '<button type="button" class="attn-more" id="attnMore">'+
          (attnOpen ? '접기' : (rest+'개 더 보기'))+'</button>'
        : '');

    var moreBtn = document.getElementById('attnMore');
    if (moreBtn) moreBtn.addEventListener('click', function(e){
      e.stopPropagation(); attnOpen = !attnOpen; renderAttention();
    });

    Array.prototype.forEach.call(listEl.querySelectorAll('.attn-row'), function(r){
      r.addEventListener('click', function(){
        selectedProduct = r.getAttribute('data-p');
        renderSparkGrid(); renderDetail();
        var d = document.getElementById('detailCard');
        if (d) d.scrollIntoView({behavior:'smooth', block:'start'});
      });
    });
  }

  // ── 오늘의 핵심 ──────────────────────────────────────────────────
  // 매일 보는 건 결국 두 가지다.
  //   1) 갑자기 출고가 뛴 게 뭐냐
  //   2) 다섯 달 안에 재고가 바닥날 게 뭐냐
  // 재고 판정은 '계절평균 +15%'(낙관) 시나리오로 본다. 잘 팔릴 때를 기준으로 잡아야
  // 발주가 늦지 않기 때문이다. 평균으로 보면 이미 늦은 뒤에 알림이 뜬다.
  var SHORT_MONTHS = 5;      // 몇 달 안쪽을 '임박'으로 볼지
  var SHORT_URGENT = 2;      // 이 안쪽은 '긴급'으로 따로 묶는다
  var SURGE_MIN = 1.5;       // (참고용) 지난달의 몇 배부터 '급등'으로 볼지
  var SURGE_DAY_RATIO = 0.5; // 하루 출고가 그 달 예측의 이 비율 이상이면 급등
  var SHORT_MAX = 6;         // 재고 바닥 목록에 한 번에 보여줄 최대 개수

  function safetyScenario(){
    for (var i = 0; i < SCENARIOS.length; i++){
      if (SCENARIOS[i].key === 'optimistic') return SCENARIOS[i];
    }
    return SCENARIOS[1];
  }

  // 이번 달 일별 기록 → '이 속도면 월말에 얼마'
  function paceOf(p){
    var byMonth = (dailyData || {})[p] || {};
    var dm = Object.keys(byMonth).sort();
    if (!dm.length) return null;
    var m = dm[dm.length - 1];
    var days = Object.keys(byMonth[m]).map(Number).sort(function(a, b){ return a - b; });
    var d = days[days.length - 1];
    if (!d || d < 3) return null;                       // 며칠 안 지났으면 판단 못 한다
    var soFar = (byMonth[m][String(d)] || {}).real || 0;
    var prev = (history[p] || {})[numYm(ymNum(m) - 1)] || 0;
    var pace = soFar * (30 / d);
    return {ym:m, day:d, soFar:soFar, pace:Math.round(pace), prev:prev,
            ratio: prev > 0 ? pace / prev : null,
            series: days.map(function(x){ return (byMonth[m][String(x)] || {}).real || 0; })};
  }

  function sparkSVG(vals, color){
    if (!vals || vals.length < 2) return '<span class="today-spark"></span>';
    var w = 64, h = 26, pad = 3;
    var mx = Math.max.apply(null, vals), mn = Math.min.apply(null, vals);
    var rng = (mx - mn) || 1;
    var pts = vals.map(function(v, i){
      var x = pad + i * (w - pad * 2) / (vals.length - 1);
      var y = h - pad - ((v - mn) / rng) * (h - pad * 2);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    return '<svg class="today-spark" viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true">' +
           '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.6" ' +
           'stroke-linejoin="round" stroke-linecap="round"/></svg>';
  }

  // 하루 만에 그 달 예측을 얼마나 써버렸는지 본다.
  // 예전엔 '이번 달 페이스가 지난달의 1.5배'였는데, 성수기(8~10월)엔 거의 전 품목이
  // 걸려서 목록이 20개 넘게 나왔다. 그래서 '하루치 튐'만 남긴다.
  function dayJumpOf(p){
    var byMonth = (dailyData || {})[p] || {};
    var dm = Object.keys(byMonth).sort();
    if (!dm.length) return null;
    var m = dm[dm.length - 1];
    var days = Object.keys(byMonth[m]).map(Number).sort(function(a, b){ return a - b; });
    if (days.length < 2) return null;
    var last = days[days.length - 1], prev = days[days.length - 2];
    var lastReal = (byMonth[m][String(last)] || {}).real || 0;
    var prevReal = (byMonth[m][String(prev)] || {}).real || 0;
    var inc = lastReal - prevReal;
    if (inc <= 0) return null;
    var fc;
    try { fc = forecastForScenario(p, safetyScenario(), fcStateByProduct[p] || defaultFcState(p)); }
    catch(e){ return null; }
    var plan = (fc && fc.values && fc.values[m]) || 0;
    if (!plan) return null;
    return {p:p, month:m, day:last, inc:inc, plan:plan, ratio:inc / plan, cum:lastReal,
            series: days.map(function(x){ return (byMonth[m][String(x)] || {}).real || 0; })};
  }
  function todaySurges(){
    var all = [];
    products.forEach(function(p){
      if (!activeBrands[brandOf(p)]) return;
      var x = dayJumpOf(p);
      if (x) all.push(x);
    });
    all.sort(function(a, b){ return b.ratio - a.ratio; });
    var hit = all.filter(function(x){ return x.ratio >= SURGE_DAY_RATIO; });
    // 기준을 넘는 게 하나도 없으면 그날 가장 크게 나간 것 하나만 참고로 보여준다
    return hit.length ? hit.slice(0, 5) : all.slice(0, 1);
  }

  function stockoutSoon(){
    var sc = safetyScenario(), cm = currentYM(), out = [];
    products.forEach(function(p){
      if (!activeBrands[brandOf(p)]) return;
      var st = fcStateByProduct[p] || defaultFcState(p);
      var fc;
      try { fc = forecastForScenario(p, sc, st); } catch(e){ return; }
      var proj = computeStockProjection(p, fc);
      if (!proj || !proj.stockout) return;
      var away = monthsBetween(cm, proj.stockout);
      if (away == null || away > SHORT_MONTHS) return;
      out.push({p:p, when:proj.stockout, away:away, short:proj.worstShort || 0,
                urgent: away <= SHORT_URGENT});
    });
    // 급한 것(2개월 안) 먼저, 그다음 5개월 안. 너무 길면 화면에서 안 읽히므로 잘라 보여준다.
    out.sort(function(a, b){ return (a.away - b.away) || (b.short - a.short); });
    return out;
  }

  function awayText(n){
    if (n <= 0) return '이번 달';
    if (n === 1) return '다음 달';
    return n + '개월 뒤';
  }

  function renderToday(){
    var sEl = document.getElementById('surgeList');
    var kEl = document.getElementById('shortList');
    if (!sEl || !kEl) return;

    var surges = todaySurges();
    var shorts = stockoutSoon();

    var asOf = document.getElementById('todayAsOf');
    if (asOf){
      var anyPace = null;
      for (var i = 0; i < products.length && !anyPace; i++) anyPace = paceOf(products[i]);
      asOf.textContent = anyPace ? (anyPace.ym + ' ' + anyPace.day + '일까지 반영') : '';
    }
    var urgent = shorts.filter(function(r){ return r.urgent; });
    var later = shorts.filter(function(r){ return !r.urgent; });
    var shown = urgent.concat(later).slice(0, SHORT_MAX);

    var sc = document.getElementById('surgeCnt'), kc = document.getElementById('shortCnt');
    if (sc) sc.textContent = surges.length ? (surges.length + '개') : '';
    if (kc) kc.textContent = shorts.length ? (urgent.length + ' / ' + shorts.length + '개') : '';

    sEl.innerHTML = surges.length ? surges.map(function(x){
      var pct = Math.round(x.ratio * 100);
      return '<div class="today-row" data-p="' + escapeXML(x.p) + '">' +
        '<div class="today-nm"><b>' + escapeXML(x.p) + '</b>' +
        '<span>' + x.day + '일 하루 ' + fmt(x.inc) + '개 · 그 달 예측 ' + fmt(x.plan) + '개</span></div>' +
        sparkSVG(x.series, 'var(--rh-up)') +
        '<div class="today-fig"><b>' + pct + '%</b><span>하루에 소진</span></div>' +
        '<span class="today-badge up">+' + fmt(x.inc) + '</span></div>';
    }).join('') : '<div class="today-empty">하루 만에 크게 빠져나간 품목이 없어요.</div>';

    kEl.innerHTML = (shown.length ? shown.map(function(r){
      return '<div class="today-row" data-p="' + escapeXML(r.p) + '">' +
        '<div class="today-nm"><b>' + escapeXML(r.p) + '</b>' +
        '<span>' + escapeXML(r.when) + '부터 · 최대 ' + fmt(r.short) + '개 모자람</span></div>' +
        '<div class="today-fig"><b>' + escapeXML(awayText(r.away)) + '</b><span>바닥나는 시점</span></div>' +
        '<span class="today-badge ' + (r.urgent ? 'down' : 'warn') + '">' +
          (r.urgent ? '긴급' : '주의') + '</span></div>';
    }).join('') : '<div class="today-empty">다섯 달 안에 바닥나는 품목이 없어요.</div>') +
      '<div class="today-note">' +
        (shorts.length > shown.length ? ('급한 순서로 ' + shown.length + '개만 보여요 (전체 ' + shorts.length + '개). ') : '') +
        '<b>긴급</b>은 2개월 안, <b>주의</b>는 5개월 안에 바닥나는 품목이에요. ' +
        '잘 팔릴 때 기준(<b>계절평균 +15%</b>)으로 계산했어요.</div>';

    [sEl, kEl].forEach(function(host){
      Array.prototype.forEach.call(host.querySelectorAll('.today-row'), function(r){
        r.addEventListener('click', function(){
          selectedProduct = r.getAttribute('data-p');
          renderSparkGrid(); renderDetail();
          var d = document.getElementById('detailCard');
          if (d) d.scrollIntoView({behavior:'smooth', block:'start'});
        });
      });
    });
  }

  // ── 예측 시나리오 ────────────────────────────────────────────────
  // 이 회사는 추석(8·9·10월)이 최대 성수기, 설(12·1·2월)이 그 다음이다.
  // 브랜드마다 성수기 민감도가 달라서, 계절 가중 시나리오는 그 배율을 쓴다.
  var PEAK_CHUSEOK = {8:1, 9:1, 10:1};
  var PEAK_SEOLLAL = {12:1, 1:1, 2:1};
  var BRAND_PEAK = {
    '산체스(소르바스)': 2.30,
    '보르헤스(포스티보나)': 1.64,
    '에스파뇰라': 1.59,
    '에스파뇰라 스프레이': 1.56,
    '에스테파(포스티보나 프리미엄)': 1.22
  };
  // 에스테파만 설이 추석보다 강하다
  var SEOLLAL_STRONGER = {'에스테파(포스티보나 프리미엄)': 1.55};

  var SCENARIOS = [
    {key:'conservative', name:'보수적',   method:'trend',    mult:0.85, peak:false,
     desc:'최근 추세를 낮게 잡음 — 재고가 남을지 확인할 때'},
    {key:'seasonal',     name:'계절평균',  method:'seasonal', mult:1.00, peak:false,
     desc:'그 달 과거 평균 (기본값)'},
    {key:'trend',        name:'최근추세',  method:'trend',    mult:1.00, peak:false,
     desc:'최근 N개월 평균 × 계절지수'},
    {key:'peak',         name:'성수기 반영', method:'seasonal', mult:1.00, peak:true,
     desc:'추석·설에 브랜드별 성수기 배율을 더 얹음'},
    {key:'optimistic',   name:'낙관',     method:'seasonal', mult:1.15, peak:false,
     desc:'계절평균 +15% — 물량이 부족할지 볼 때'}
  ];
  // 계절평균(추천 기본값)은 다른 곳과 통일해서 네온그린으로 — 나머지는 구분되는 색
  var SCENARIO_COLOR = {
    conservative:'var(--series-1)', seasonal:'var(--fc-neon)', trend:'var(--series-6)',
    peak:'var(--series-3)', optimistic:'var(--series-5)'
  };

  function peakBoost(p, ym){
    var b = brandOf(p);
    var m = monthOf(ym);
    var strong = SEOLLAL_STRONGER[b];
    if (strong && PEAK_SEOLLAL[m]) return strong;
    if (PEAK_CHUSEOK[m]) return BRAND_PEAK[b] || 1.0;
    if (PEAK_SEOLLAL[m]) return ((BRAND_PEAK[b] || 1.0) - 1) * 0.6 + 1;  // 설은 추석보다 약하다
    return 1.0;
  }

  function forecastForScenario(p, sc, st){
    var hist = history[p] || {};
    var cfg = {method:sc.method, scenario:String(sc.mult), window:st.window,
               months:st.months, start:st.start,
               exclude: st.exclude ? st.exclude.split(',').map(function(x){return x.trim();}).filter(Boolean) : []};
    var fc = computeForecast(hist, cfg);
    if (sc.peak){
      fc.months.forEach(function(m){
        fc.values[m] = Math.round(fc.values[m] * peakBoost(p, m));
      });
    }
    return fc;
  }

  // 시나리오별 비교 — 표 대신 라인 그래프로 겹쳐 보여준다 (직접설정 켰을 때만).
  // 계절평균(추천값)은 네온그린으로 다른 곳과 통일해서, 지금 보고 있는 값이 어디쯤인지 바로 보인다.
  function renderScenarioChart(container, p, st){
    var rows = SCENARIOS.map(function(sc){
      var fc = forecastForScenario(p, sc, st);
      return {sc:sc, fc:fc, color: SCENARIO_COLOR[sc.key] || 'var(--series-1)'};
    });
    var months = rows[0].fc.months;
    if (!months.length){ container.innerHTML = ''; return; }

    var W = 900, H = 220, padL = 52, padR = 16, padT = 16, padB = 28;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var maxV = 1;
    rows.forEach(function(r){ months.forEach(function(m){ var v=r.fc.values[m]||0; if (v>maxV) maxV=v; }); });
    var niceMax = niceCeil(maxV);
    function xAt(i){ return padL + (months.length>1 ? i/(months.length-1) : 0.5) * plotW; }
    function yAt(v){ return padT + plotH - (Math.max(0,v)/niceMax) * plotH; }

    var svg = '<svg viewBox="0 0 '+W+' '+H+'" class="chart-svg" preserveAspectRatio="none">';
    for (var g=0; g<=4; g++){
      var gv = niceMax*g/4, gy = yAt(gv);
      svg += '<line x1="'+padL+'" y1="'+gy+'" x2="'+(W-padR)+'" y2="'+gy+'" class="gridline"/>';
      svg += '<text x="'+(padL-8)+'" y="'+(gy+3)+'" class="axis-label" text-anchor="end">'+fmtCompact(gv)+'</text>';
    }
    months.forEach(function(m,i){
      svg += '<text x="'+xAt(i)+'" y="'+(H-8)+'" class="axis-label" text-anchor="middle">'+m.slice(2)+'</text>';
    });

    rows.forEach(function(r){
      var d='';
      months.forEach(function(m,i){ d += (i?'L':'M')+xAt(i).toFixed(1)+','+yAt(r.fc.values[m]||0).toFixed(1)+' '; });
      var isReco = r.sc.key === 'seasonal';
      svg += '<path d="'+d.trim()+'" fill="none" stroke="'+r.color+'" stroke-width="'+(isReco?3:2)+'" '+
             'stroke-linecap="round" stroke-linejoin="round"'+(isReco?'':' stroke-dasharray="5,3" opacity="0.85"')+'/>';
    });
    svg += '</svg>';

    var legend = rows.map(function(r){
      return '<span class="sc-leg" data-k="'+r.sc.key+'" style="color:'+r.color+'" title="'+escapeXML(r.sc.desc)+'">'+
             '● '+escapeXML(r.sc.name)+'</span>';
    }).join('');

    container.innerHTML =
      '<div class="fc-proj"><div class="fc-proj-head">'+
        '<h4>🎯 시나리오별 비교</h4>'+
        '<span class="fc-proj-sub">'+escapeXML(st.start)+'부터 '+st.months+'개월 · 이름을 누르면 그 시나리오로 바꿔요</span>'+
      '</div>'+ svg +
      '<div class="fc-proj-legend sc-legend">'+legend+'</div></div>';

    Array.prototype.forEach.call(container.querySelectorAll('.sc-leg'), function(el){
      el.addEventListener('click', function(){
        var sc = SCENARIOS.filter(function(x){ return x.key === el.getAttribute('data-k'); })[0];
        if (!sc) return;
        var panel = document.querySelector('.fc-panel');
        if (panel){
          var ms = panel.querySelector('[name=method]');
          var mv = panel.querySelector('[name=mult]');
          if (ms) ms.value = sc.method;
          if (mv) mv.value = String(sc.mult);
          ms && ms.dispatchEvent(new Event('change', {bubbles:true}));
          mv && mv.dispatchEvent(new Event('input', {bubbles:true}));
        }
        showToast(sc.name+' 시나리오로 바꿨어요');
      });
    });
  }

  function defaultFcState(p){
    var hist=history[p]||{}; var keys=Object.keys(hist).sort();
    var start;
    if (!keys.length){
      start = currentYM();
    } else {
      var last = keys[keys.length-1];
      // 이번 달은 아직 안 끝났다. 그 달 실적은 '지금까지'일 뿐이라
      // 예측을 다음 달부터 시작하면 끊겨 보인다. 이번 달부터 예측한다.
      start = (last === currentYM()) ? last : numYm(ymNum(last)+1);
    }
    return {method:'seasonal', scenario:'1.0', window:8, months:8, start:start, exclude:''};
  }

  // 수요예측 탭에서 부르는 기본 진입점
  function drawDetailChart(){
    var chartEl = document.getElementById('detailChart');
    var noteEl = document.getElementById('detailLegendNote');
    if (!selectedProduct){ chartEl.innerHTML=''; noteEl.textContent=''; return; }
    drawForecastChart(selectedProduct, chartEl, noteEl);
  }

  // 상품 하나의 실적+예측+재고 차트. 수요예측 / 신상품관리 두 화면이 같이 쓴다.
  function drawForecastChart(p, chartEl, noteEl){
    if (!p || !chartEl) return;

    var prodForecasts = forecasts[p] || [];
    var invEntries = inventoryData[p] || [];
    var monthsSet = {};
    Object.keys(history[p]||{}).forEach(function(m){monthsSet[m]=true;});
    HIST_MONTHS.forEach(function(m){monthsSet[m]=true;});
    prodForecasts.forEach(function(e){ Object.keys(e.values).forEach(function(m){monthsSet[m]=true;}); });
    if (fcPreview){ Object.keys(fcPreview.values).forEach(function(m){monthsSet[m]=true;}); }
    invEntries.forEach(function(e){ monthsSet[e.month]=true; });
    var allM = Object.keys(monthsSet).sort();

    var bars = null;
    if (invEntries.length){
      var bmap={}, bmax=0;
      invEntries.forEach(function(e){
        bmap[e.month] = {base:e.base, inbound:e.inbound, qty:e.qty, shipment:e.shipment, is_snapshot:e.is_snapshot};
        if (e.qty>bmax) bmax=e.qty;
      });
      bars = {map:bmap, max:bmax};
    }

    // 예측 구간의 재고 막대는 "출고 안 빠진 상태"로 남아 있어서 실제와 어긋난다.
    // 미리보기 예측이 있으면 그 출고량을 차감한 잔여재고로 덮어씌운다.
    if (bars && fcPreview){
      var proj = computeStockProjection(p, fcPreview, stockDelaySeg);
      if (proj){
        proj.rows.forEach(function(r){
          var inb = r.inbound || null;
          // 남은 재고(0 밑으로는 안 내려감) 위에 그 달 입고분을 '실제 수량 그대로' 얹는다.
          // 예전엔 월말 재고가 0 이하면 입고 막대 높이가 0 이 돼서, 물건이 들어온 달인데도
          // 막대가 아예 안 보였다 (재고가 마이너스인 달의 입고가 통째로 사라짐).
          var left = Math.max(0, r.close);
          bars.map[r.month] = {
            base: left,
            inbound: inb,
            qty: left + (inb || 0),
            close: r.close,          // 음수까지 그대로 (툴팁에 실제 값 표시용)
            shipment: r.out,
            is_snapshot: false,
            projected: true
          };
          if (bars.map[r.month].qty > bars.max) bars.max = bars.map[r.month].qty;
        });
      }
    }

    // '실제'는 오해를 부른다 — 이번 달 값은 재고표를 받은 날까지의 누적일 뿐이다.
    var asOf = latestSnapshotDate(p);
    var actualName = asOf ? (asOf+' 기준 출고량') : '갱신날짜 기준 출고량';
    var series = [{name:actualName, color:'var(--series-1)', values: history[p]}];
    // 저장된 예측선은 그리지 않는다 — 지금 설정한 예측 하나만 보이는 게 읽기 쉽다.
    // (저장된 값 자체는 data/forecasts.json 에 그대로 남아 있다)
    if (fcPreview){
      // 실적선과 붙어 보이게, 예측 시작 바로 앞 달의 실적을 이음점으로 하나 넣는다.
      // (이번 달은 아직 안 끝나서 예측이 그 달부터 시작하는데, 그러면 앞이 끊겨 보인다)
      var pv = {};
      Object.keys(fcPreview.values).forEach(function(m){ pv[m] = fcPreview.values[m]; });
      var fm = Object.keys(pv).sort();
      if (fm.length){
        var prev = numYm(ymNum(fm[0]) - 1);
        var hv = (history[p]||{})[prev];
        if (hv != null && pv[prev] == null) pv[prev] = hv;
      }
      series.push({name:'예측 미리보기'+(fcUnlocked?' (끌어서 수정)':''), color:'var(--fc-neon)', dashed:true,
                   dots:true, glow:true, draggable:fcUnlocked, values: pv, joinFrom: fm.length?fm[0]:null});
    }

    renderLineChart(chartEl, series, allM, {
      height:320, directLabel:false, bars:bars, product:p,
      onDrag: (fcUnlocked && fcPreview) ? function(m, v, final){
        if (fcPreview.values[m]==null) return;
        fcPreview.values[m] = v;
        (fcManual[p] = fcManual[p] || {})[m] = v;
        if (final) { if (fcOnManual) fcOnManual(); }   // 최종값에서만 전체 다시 그림
        else if (fcOnManualLive) fcOnManualLive();     // 끄는 중엔 요약·재고막대만 갱신
      } : null
    });
    if (noteEl){
      var base = prodForecasts.length ? ('저장된 예측 '+prodForecasts.length+'건 중 최근 2건 + 현재 설정 미리보기를 표시했어요.') : '위 패널에서 예측 식을 조절하면 네온 그린 곡선으로 미리보기가 그려져요.';
      noteEl.textContent = base + (bars ? ' 흐린 막대는 재고(오른쪽 축), 그 위 다른 색은 추가발주 입고분이에요. 점선 테두리의 네온 막대는 예측 출고를 뺀 예상 재고예요.' : '')
        + (fcUnlocked ? ' 🔓 잠금해제 상태 — 네온 점을 위아래로 끌면 그 달 예측값이 바뀝니다.' : '');
    }
    var dbar = document.getElementById('delayBar');
    if (dbar){
      dbar.style.display = bars ? '' : 'none';   // 재고 막대가 없으면 밀어 볼 것도 없다
      if (bars && typeof applyDelayUI === 'function') applyDelayUI();
    }
  }

  var SEG_LABEL = {early:'초', mid:'중순', late:'말'};

  // ── 입고 지연 시뮬레이션 ─────────────────────────────────────────
  // 노란 '입고예정' 물량을 블럭처럼 오른쪽으로 밀어 보면서, 컨테이너가 늦어질 때
  // 재고가 언제 바닥나는지 눈으로 본다. 한 칸 = 초·중·말 한 구간 ≈ 1주.
  var stockDelaySeg = 0;
  var DELAY_MAX = 6;
  function delayText(n){ return n ? ('입고 ' + n + '주 지연') : '지연 없음'; }
  function applyDelayUI(){
    var track = document.getElementById('delayTrack');
    var block = document.getElementById('delayBlock');
    var val = document.getElementById('delayVal');
    var note = document.getElementById('delayNote');
    if (!track || !block) return;
    var frac = stockDelaySeg / DELAY_MAX;
    block.style.left = (frac * (track.clientWidth - block.offsetWidth)) + 'px';
    if (val) val.textContent = delayText(stockDelaySeg);
    if (val) val.className = 'delaybar-val' + (stockDelaySeg ? ' on' : '');
    if (note){
      var p = selectedProduct, msg = '';
      if (!stockDelaySeg){
        note.textContent = '📦 블럭을 오른쪽으로 끌어 보세요. 한 칸이 약 1주예요 — 컨테이너가 밀리면 재고가 어떻게 되는지 바로 보여드려요.';
        return;
      }
      if (p){
        var fc = fcPreview;
        if (fc){
          var a = computeStockProjection(p, fc, 0);
          var b = computeStockProjection(p, fc, stockDelaySeg);
          if (a && b){
            if (!b.stockout) msg = '이만큼 밀려도 바닥나지 않아요.';
            else if (!a.stockout) msg = '지연이 없으면 괜찮지만, 이만큼 밀리면 ' + b.stockout + '부터 최대 ' + fmt(b.worstShort) + '개 모자라요.';
            else if (a.stockout !== b.stockout) msg = '바닥나는 시점이 ' + a.stockout + ' → ' + b.stockout + ' 로 바뀌어요.';
            else msg = b.stockout + '부터 모자란 양이 ' + fmt(a.worstShort) + '개 → ' + fmt(b.worstShort) + '개 로 늘어요.';
          }
        }
      }
      note.textContent = msg;
    }
  }
  function setDelay(n){
    n = Math.max(0, Math.min(DELAY_MAX, Math.round(n)));
    if (n === stockDelaySeg){ applyDelayUI(); return; }
    stockDelaySeg = n;
    drawDetailChart();
    applyDelayUI();
  }
  (function initDelayDrag(){
    var track = document.getElementById('delayTrack');
    var block = document.getElementById('delayBlock');
    var reset = document.getElementById('delayReset');
    if (!track || !block) return;
    var dragging = false;
    function segFromX(clientX){
      var r = track.getBoundingClientRect();
      var span = r.width - block.offsetWidth;
      if (span <= 0) return 0;
      return ((clientX - r.left - block.offsetWidth / 2) / span) * DELAY_MAX;
    }
    function down(e){ dragging = true; block.classList.add('drag'); move(e); e.preventDefault(); }
    function move(e){
      if (!dragging) return;
      var x = (e.touches ? e.touches[0].clientX : e.clientX);
      setDelay(segFromX(x));
    }
    function up(){ dragging = false; block.classList.remove('drag'); }
    block.addEventListener('mousedown', down);
    block.addEventListener('touchstart', down, {passive:false});
    window.addEventListener('mousemove', move);
    window.addEventListener('touchmove', move, {passive:false});
    window.addEventListener('mouseup', up);
    window.addEventListener('touchend', up);
    // 트랙을 눌러도 그 자리로 옮긴다
    track.addEventListener('click', function(e){ if (e.target !== block) setDelay(segFromX(e.clientX)); });
    if (reset) reset.addEventListener('click', function(){ setDelay(0); });
    window.addEventListener('resize', applyDelayUI);
  })();

  // ── 가장 최근 실사 기준 현재고 ──
  // 마지막으로 세어본 값에서 그 뒤 실적(출고)·입고를 반영해 '지금' 재고를 추정한다.
  function currentStock(p){
    var raw = inventoryRaw[p] || {};
    var stock = raw.stock || {}, sday = raw.stock_day || {}, swip = raw.stock_wip || {};
    var ship = raw.shipments || {}, inb = raw.inbound || {};
    var nowYM = currentYM();
    // 앞으로의 계획값(미래 월 실사)은 '현재고'가 아니므로 오늘까지의 것만 본다
    var months = Object.keys(stock).filter(function(m){ return m <= nowYM; }).sort();
    if (!months.length) return null;

    var last = months[months.length-1];
    var qty = stock[last], day = sday[last] || 1;
    var wip = swip[last] || 0;   // 실사값(qty)엔 작업중 물량도 포함돼 있다 (ERP 기준)

    // 실사한 달 이후 ~ 이번 달까지 출고를 빼고 입고를 더한다
    var m = numYm(ymNum(last)+1), moved = 0, gotIn = 0, wentOut = 0;
    while (ymNum(m) <= ymNum(nowYM)){
      var o = ship[m] || 0, i = inb[m] || 0;
      wentOut += o; gotIn += i; moved += 1;
      m = numYm(ymNum(m)+1);
    }
    var now = qty + gotIn - wentOut;
    var dayStock = now - wip;   // 당일재고 = 실제재고 − 작업중(선물세트 조립 등으로 묶인 물량)
    var hold = holdOf(p, nowYM);

    // 실사 이후 며칠 지났는지
    var d0 = new Date(parseInt(last.slice(0,4),10), parseInt(last.slice(5,7),10)-1, day);
    var days = Math.max(0, Math.round((todayD() - d0) / 86400000));

    return {asof:last, day:day, snap:qty, now:now, dayStock:dayStock, wip:wip,
            hold:(hold?hold.total:0), out:wentOut, inb:gotIn,
            months:moved, days:days, stale:(days > 45)};
  }

  function renderStockNow(p){
    var host = document.getElementById('stockNow');
    if (!host) return;
    var s = p ? currentStock(p) : null;
    if (!s){
      host.innerHTML = p
        ? '<div class="snow snow-none">재고 실사값이 없어요 — 아래 <b>재고 실사</b> 칸에 지금 재고를 넣으면 현재고가 여기 표시돼요.</div>'
        : '';
      return;
    }
    var moved = (s.out || s.inb);
    var extraBits = [];
    if (s.wip) extraBits.push('작업중 '+fmt(s.wip)+'개');
    if (s.hold) extraBits.push('홀딩 '+fmt(s.hold)+'개');
    host.innerHTML =
      '<div class="snow'+(s.stale?' warn':'')+'">'+
        '<div class="snow-main">'+
          '<span class="snow-lbl">현재고</span>'+
          '<span class="snow-val">'+fmt(s.dayStock)+'</span><span class="snow-unit">개</span>'+
          (extraBits.length ? '<span class="snow-extra">('+extraBits.join(' · ')+')</span>' : '')+
        '</div>'+
        '<div class="snow-side">'+
          '<div class="snow-r"><span>최근 실사</span><b>'+escapeXML(s.asof)+'-'+
            ('0'+s.day).slice(-2)+'</b> <i>'+fmt(s.snap)+'개</i></div>'+
          (moved
            ? '<div class="snow-r"><span>그 뒤 반영</span>'+
              (s.inb?'<b class="up">+'+fmt(s.inb)+'</b> ':'')+
              (s.out?'<b class="dn">-'+fmt(s.out)+'</b>':'')+'</div>'
            : '<div class="snow-r"><span>그 뒤 변동</span><b>없음</b></div>')+
          '<div class="snow-r"><span>경과</span><b>'+s.days+'일</b></div>'+
        '</div>'+
        (s.stale?'<div class="snow-note">실사한 지 '+s.days+'일 지났어요. 한 번 세어보시면 예측이 더 정확해집니다.</div>':'')+
      '</div>';
  }


  // ── 예측 출고량을 현재고에서 차감해 월별 잔여재고를 뽑아낸다 ──
  // 출발점 = 예측 시작월 직전의 재고(=사장님이 넣은 재고 실사값 기준으로 계산된 값),
  // 그 뒤로 매달 (기존 입고예정 + 예측 출고 차감) 을 굴린다.
  // 입고를 n 칸(초→중→말, 한 칸이 약 10일) 뒤로 민다.
  // '입고가 일주일 넘게 늦으면 어떻게 되나' 를 보려고 쓴다. 말(late)에서 더 밀리면 다음 달 초.
  var SEG_ORDER = ['early', 'mid', 'late'];
  function delayInbound(inbBy, inbWhen, n){
    var by = {}, when = {};
    Object.keys(inbBy).forEach(function(m){
      var idx = SEG_ORDER.indexOf(inbWhen[m] || 'mid');
      if (idx < 0) idx = 1;
      idx += n;
      var mm = m;
      while (idx > 2){ idx -= 3; mm = numYm(ymNum(mm) + 1); }
      by[mm] = (by[mm] || 0) + inbBy[m];
      // 같은 달에 둘이 겹치면 늦은 쪽에 맞춘다 (보수적으로 본다)
      var cur = SEG_ORDER.indexOf(when[mm] || 'early');
      when[mm] = SEG_ORDER[Math.max(cur, idx)];
    });
    return {by: by, when: when};
  }

  function computeStockProjection(p, fc, delaySeg){
    var entries = inventoryData[p] || [];
    if (!entries.length || !fc || !fc.months.length) return null;

    var inbBy = {}, inbWhen = {};
    entries.forEach(function(e){
      if (e.inbound){ inbBy[e.month] = e.inbound; inbWhen[e.month] = e.inbound_at || 'mid'; }
    });
    if (delaySeg){
      var d = delayInbound(inbBy, inbWhen, delaySeg);
      inbBy = d.by; inbWhen = d.when;
    }
    // 예측 구간 안에 재고 실사가 있으면 그 달 '그 날짜부터' 그 값으로 다시 시작한다.
    // 실사값엔 작업중(선물세트 조립 등) 물량이 섞여 있는데, 그건 조립 끝나면 이 상품
    // 재고로 안 돌아오니 재고소진 계산에서는 뺀다 (화면에 보여주는 '현재 재고'와는 다른 값).
    var snapIn = {}, snapDay = {}, snapWip = {};
    entries.forEach(function(e){
      if (e.is_snapshot){
        snapDay[e.month] = e.snap_day || 1;
        snapWip[e.month] = e.wip || 0;
        snapIn[e.month] = e.qty - snapWip[e.month];
      }
    });
    // 실사한 날이 그 달의 몇 번째 칸(초/중순/말)에 해당하는지
    function segOfDay(d){ return d <= 10 ? 0 : (d <= 20 ? 1 : 2); }

    var startNum = ymNum(fc.months[0]);
    var anchor = null;
    entries.forEach(function(e){
      if (ymNum(e.month) < startNum) anchor = e;      // 예측 시작 직전까지의 마지막 재고
    });
    if (!anchor) anchor = entries[0];
    var anchorUsable = anchor.qty - (anchor.wip || 0);   // 작업중 뺀, 실제로 팔 수 있는 양

    var running = anchorUsable;   // 예측 출고를 차감한 값
    var plain = anchorUsable;     // 차감 안 한 값 (재고실사 + 입고예정만 반영)
    var rows = [], stockout = '', worst = null, cumOut = 0;
    var midWorst = null, midShort = '';   // 월 안(초·중·말)에서만 생기는 구멍
    fc.months.forEach(function(m){
      var hasSnap = (snapIn[m] != null);
      var snapSeg = hasSnap ? segOfDay(snapDay[m]) : -1;
      // 실사가 월초(1~10일)면 그 달 시작부터 그 값으로 맞춘다
      if (hasSnap && snapSeg === 0){ running = snapIn[m]; plain = snapIn[m]; }
      var open = running, out = fc.values[m] || 0, inb = inbBy[m] || 0;
      var when = inbWhen[m] || 'mid';

      // ── 이미 나간 몫은 두 번 빼지 않는다 ──────────────────────────
      // 재고 실사값은 '그날까지 이미 나간 출고'가 빠진 뒤의 숫자다.
      // ERP 금월 출고수량에는 작업중·홀딩 물량이 이미 잡혀 있으므로,
      // 실사가 있는 달에 예측 출고를 통째로 또 빼면 같은 물량을 두 번 빼게 된다.
      // 그래서 실사가 있는 달은 '남은 예상 출고'(예측 − 이미 나간 양)만 뺀다.
      var doneOut = hasSnap ? ((history[p] || {})[m] || 0) : 0;
      var restOut = hasSnap ? Math.max(out - doneOut, 0) : out;

      // ── 월 안을 초·중·말 셋으로 쪼갠다 ──
      // 출고는 남은 기간에 고르게 흐른다고 보고 나누고, 입고는 지정한 시점에 한 번에 들어온다.
      // 실사가 있으면 실사한 칸부터 남은 몫만 나눠 뺀다 (그 앞 칸은 실사값이 대신한다).
      var sub = [], run = open;
      var firstSeg = hasSnap ? snapSeg : 0, nSeg = 3 - firstSeg, segOut = [0, 0, 0];
      if (hasSnap){
        var perR = Math.round(restOut / nSeg);
        for (var sj = firstSeg; sj < 3; sj++) segOut[sj] = perR;
        segOut[2] = restOut - perR * (nSeg - 1);
      } else {
        var perF = Math.round(out / 3);
        segOut = [perF, perF, out - perF * 2];
      }
      ['early','mid','late'].forEach(function(seg, si){
        // 실사한 날이 이 칸이면, 세어본 값으로 여기서 다시 시작
        var reset = (hasSnap && snapSeg === si);
        if (reset && si > 0) run = snapIn[m];
        var gotIn = (when === seg) ? inb : 0;
        var so = segOut[si];
        var o2 = run;
        run = o2 + gotIn - so;
        sub.push({seg:seg, open:o2, inbound:gotIn, out:so, close:run,
                  snap: reset ? snapIn[m] : null,
                  snapDay: reset ? snapDay[m] : null,
                  shortage: run < 0 ? -run : 0});
        if (run < 0){
          if (!midShort) midShort = m + '/' + seg;
          if (!midWorst || run < midWorst.v) midWorst = {month:m, seg:seg, v:run};
        }
      });

      running = run;
      plain = plain + inb;
      cumOut += out;
      if (running < 0){
        if (!stockout) stockout = m;
        if (!worst || running < worst.short) worst = {month:m, short:running};
      }
      rows.push({month:m, open:open, out:out, done:doneOut, outRest:restOut,
                 inbound:inb, when:when, sub:sub,
                 snap: hasSnap ? snapIn[m] : null, snapDay: hasSnap ? snapDay[m] : null,
                 close:running, plain:plain, cumOut:cumOut,
                 shortage: running < 0 ? -running : 0,
                 // 월말은 멀쩡한데 월 중간에만 구멍나는 달 (입고가 늦어서 생기는 문제)
                 midOnly: (running >= 0 && sub.some(function(s){ return s.close < 0; }))});
    });
    return {anchorMonth:anchor.month, anchorQty:anchor.qty, anchorUsable:anchorUsable,
            anchorWip: anchor.wip || 0, isSnapshot:!!anchor.is_snapshot,
            rows:rows, stockout:stockout,
            worstMonth: worst ? worst.month : '', worstShort: worst ? -worst.short : 0,
            midShort: midShort, midWorst: midWorst ? -midWorst.v : 0,
            midWorstAt: midWorst ? (midWorst.month+' '+SEG_LABEL[midWorst.seg]) : ''};
  }

  // 한 달을 초·중·말로 펼쳐 보기 — 입고 시점 때문에 월 중간에 구멍이 나는지 확인용

  // ── 월 안에서 출고가 어떻게 늘어가는지 (재고표를 줄 때마다 쌓인 값) ──
  var DAILY_FROM = '2026-08';   // 이 달부터 기록을 모으기 시작했다

  // 그 달 재고표에 잡힌 홀딩 (가장 최근 스냅샷 기준)
  // {total, cut, list:[{q,d,k}]} — cut 은 이번 달 출고에서 실제로 뺀 양
  function holdOf(p, ym){
    if (!p) return null;
    var mm = ((dailyData||{})[p] || {})[ym];
    if (!mm) return null;
    var days = Object.keys(mm).map(Number).sort(function(a,b){ return a-b; });
    if (!days.length) return null;
    var v = mm[String(days[days.length-1])] || {};
    if (!v.hold) return null;
    return {total: v.hold, cut: (v.hold_cut==null ? v.hold : v.hold_cut), list: v.holds || []};
  }

  // 그 달 입고예정 날짜를 '9/9' 처럼. 한 건이면 툴팁에 수량이 이미 따로 표시되니
  // 날짜만 보여준다 — 두 건 이상이면 어느 게 얼마인지 알아야 해서 수량도 같이 보여준다.
  // 예: 한 건 '9/9', 여러 건 '9/9 12,240개, 10/16 22,680개'
  function inboundDetail(p, ym){
    if (!p) return '';
    var det = ((inventoryRaw[p] || {}).inbound_detail || {})[ym];
    if (!det || !det.length) return '';
    if (det.length === 1) return det[0].d.slice(5).replace('-','/');
    return det.map(function(x){
      return x.d.slice(5).replace('-','/')+' '+fmt(x.q)+'개';
    }).join(', ');
  }

  // '1,000개 9/30 예정' 처럼 짧게
  function holdText(h){
    if (!h) return '';
    if (h.list && h.list.length){
      return h.list.map(function(x){
        var d = x.d ? (x.d.slice(5).replace('-','/')+' 예정') : '날짜없음';
        return fmt(x.q)+'개 '+d;
      }).join(', ');
    }
    return fmt(h.total)+'개';
  }

  // 이 상품의 가장 최근 재고표 날짜 (예: '2026-08-12')
  function latestSnapshotDate(p){
    var byMonth = (dailyData||{})[p] || {};
    var ms = Object.keys(byMonth).sort();
    if (!ms.length) return '';
    var m = ms[ms.length-1];
    var days = Object.keys(byMonth[m]).map(Number).sort(function(a,b){ return a-b; });
    if (!days.length) return '';
    var d = days[days.length-1];
    return m + '-' + (d<10?'0':'') + d;
  }

  // 월 안에서 하루하루 얼마나 나갔는지 — 신상품관리 탭의 '하루하루' 표와 같은 방식.
  // 재고표를 줄 때마다 쌓인 '그날까지 누적'에서 하루치 차이를 뽑아 숫자로 보여준다.
  function renderDailyShip(container, p){
    var byMonth = (dailyData||{})[p] || {};
    var months = Object.keys(byMonth).filter(function(m){ return m >= DAILY_FROM; }).sort();

    if (!months.length){
      container.innerHTML = '<div class="fc-proj"><div class="fc-proj-head">'+
        '<h4>📅 하루하루 얼마나 나갔나</h4></div>'+
        '<div class="empty-note" style="padding:14px 0">아직 쌓인 기록이 없어요. '+
        'ERP 재고표를 올릴 때마다 그날 값이 쌓여요.</div></div>';
      return;
    }

    var pts = [];
    months.forEach(function(m){
      Object.keys(byMonth[m]).map(Number).sort(function(a,b){ return a-b; }).forEach(function(d){
        pts.push({ym:m, d:d, key:m.slice(5)+'/'+(d<10?'0':'')+d,
                  cum:(byMonth[m][String(d)]||{}).real || 0});
      });
    });
    var per = {}, days = [];
    pts.forEach(function(pt, i){
      // 달이 바뀌면 누적이 0부터 다시 시작하므로 그 날은 누적값 자체가 하루치
      var prev = (i>0 && pts[i-1].ym===pt.ym) ? pts[i-1].cum : 0;
      per[pt.key] = pt.cum - prev;
      days.push(pt.key);
    });

    var tot = days.reduce(function(a,d){ return a + (per[d]||0); }, 0);
    var sub = days.length===1
      ? '재고표 1장뿐이라 첫날 누적만 있어요. 내일 것부터 하루치가 보여요.'
      : (days[0]+' ~ '+days[days.length-1]+' · 하루에 몇 개씩 나갔는지');

    var head = '<tr><th></th>'+days.map(function(d){ return '<th>'+d+'</th>'; }).join('')+'<th>합계</th></tr>';
    var tds = days.map(function(d){
      var v = per[d];
      var cls = v>0 ? 'np-pos' : (v<0 ? 'np-neg' : 'np-zero');
      return '<td class="num '+cls+'">'+(v===0?'0':fmt(v))+'</td>';
    }).join('');
    var body = '<tr><td class="np-nm">출고</td>'+tds+'<td class="num"><b>'+fmt(tot)+'</b></td></tr>';

    container.innerHTML =
      '<div class="fc-proj">'+
        '<div class="fc-proj-head">'+
          '<h4>📅 하루하루 얼마나 나갔나</h4>'+
          '<span class="fc-proj-sub">'+sub+'</span>'+
        '</div>'+
        '<div class="np-daily-wrap"><table class="np-daily"><thead>'+head+'</thead><tbody>'+body+'</tbody></table></div>'+
      '</div>';
  }

  function renderStockProjection(container, proj){
    // 예전의 '예측 출고 반영 재고 추이' 막대그래프는 뺐다.
    // 대신 이 자리에 월 안에서의 출고량 변화를 그린다.
    renderDailyShip(container, selectedProduct);
    return;
  }


  function renderForecastPanel(container, p, redraw){
    if (!fcStateByProduct[p]) fcStateByProduct[p] = defaultFcState(p);
    var st = fcStateByProduct[p];
    var drawChart = redraw || drawDetailChart;
    function opt(v,label,cur){ return '<option value="'+v+'"'+(v===cur?' selected':'')+'>'+label+'</option>'; }
    var multNow = parseFloat(st.scenario);
    if (!isFinite(multNow)) multNow = 1.0;   // 예전에 저장된 base/optimistic 값도 1.0으로 받아줌
    function chip(kind,v,label){ return '<button type="button" class="fc-chip" data-'+kind+'="'+v+'">'+label+'</button>'; }
    var methodLabel = {seasonal:'계절평균', trend:'최근추세'}[st.method] || st.method;
    container.innerHTML =
      '<div class="fc-panel">' +
        '<div class="fc-panel-title"><span class="fc-dot"></span>예측 식 설정</div>' +
        '<div class="fc-reco">' +
          '<span class="fc-reco-line">'+methodLabel+' · ×'+multNow.toFixed(2)+' · '+st.months+'개월 예측'+
            ' <span class="fc-reco-tag">(클로드추천)</span></span>' +
          '<button type="button" class="fc-custom-btn'+(fcCustomOpen?' on':'')+'">'+
            (fcCustomOpen?'접기 ▲':'⚙ 직접설정')+'</button>' +
        '</div>' +
        '<div class="fc-quick"'+(fcCustomOpen?'':' hidden')+'>' +
          '<div class="fc-qgroup"><span>기간</span>' +
            chip('mo','3','3개월') + chip('mo','6','6개월') + chip('mo','8','8개월') + chip('mo','12','12개월') +
          '</div>' +
          '<button type="button" class="fc-lock'+(fcUnlocked?' on':'')+'">'+
            (fcUnlocked?'🔓 잠금해제됨 — 점을 끌어서 수정':'🔒 그래프 잠금')+'</button>' +
          '<button type="button" class="fc-manual-reset" hidden>↺ 직접수정 되돌리기</button>' +
          '<button type="button" class="fc-adv-btn">고급 설정</button>' +
        '</div>' +
        '<div class="fc-controls"'+(fcCustomOpen?'':' hidden')+'>' +
          '<div class="fc-field"><label>방식</label><select name="method">' +
            opt('seasonal','계절평균 (해당월 과거평균)', st.method) +
            opt('trend','최근추세 (최근평균×계절지수)', st.method) +
          '</select></div>' +
          '<div class="fc-field fc-multwrap">' +
            '<label>배율 <b class="fc-mult-val">×'+multNow.toFixed(2)+'</b>' +
              '<button type="button" class="fc-mult-reset" title="1배로">↺</button></label>' +
            '<input type="range" name="mult" class="fc-dial" step="0.05" min="0" max="10" value="'+multNow+'">' +
            '<div class="fc-dial-scale"><span>0</span><span>1</span><span>5</span><span>10배</span></div>' +
          '</div>' +
          '<div class="fc-field fc-window"'+(st.method==='trend'?'':' style="display:none"')+'><label>최근 N개월</label><input type="number" name="window" min="1" max="24" value="'+st.window+'"></div>' +
          '<div class="fc-field"><label>시작월</label><input type="month" name="start" value="'+st.start+'"></div>' +
          '<div class="fc-field"><label>개월수</label><input type="number" name="months" min="1" max="24" value="'+st.months+'"></div>' +
          '<div class="fc-field fc-adv"'+(st.exclude?'':' style="display:none"')+'><label>제외월 (콤마)</label><input type="text" name="exclude" placeholder="2026-03,2025-09" value="'+escapeXML(st.exclude||'')+'"></div>' +
          '<button type="button" class="fc-save">이 예측 저장</button>' +
        '</div>' +
        '<div class="fc-summary"></div>' +
        '<div class="fc-scenarios"></div>' +
        '<div class="fc-projwrap"></div>' +
      '</div>';

    var panel = container.querySelector('.fc-panel');
    var summaryEl = panel.querySelector('.fc-summary');
    var projEl = panel.querySelector('.fc-projwrap');

    // 직접설정 — 평소엔 클로드추천(계절평균·×1·8개월) 요약 한 줄만 보여주고,
    // 방식·배율·시작월 같은 입력칸은 이 버튼을 눌러야 나온다.
    panel.querySelector('.fc-custom-btn').addEventListener('click', function(){
      fcCustomOpen = !fcCustomOpen;
      var btn = panel.querySelector('.fc-custom-btn');
      panel.querySelector('.fc-quick').hidden = !fcCustomOpen;
      panel.querySelector('.fc-controls').hidden = !fcCustomOpen;
      btn.classList.toggle('on', fcCustomOpen);
      btn.textContent = fcCustomOpen ? '접기 ▲' : '⚙ 직접설정';
      paintSummary();   // 시나리오 비교 그래프를 켜고/끄기
    });

    // 고급 설정 (제외월) 접기 — 평소엔 안 보이게 해서 자주 쓰는 항목만 남긴다
    panel.querySelector('.fc-adv-btn').addEventListener('click', function(){
      var f = panel.querySelector('.fc-adv');
      f.style.display = f.style.display==='none' ? '' : 'none';
    });

    function syncChips(){
      Array.prototype.forEach.call(panel.querySelectorAll('.fc-chip'), function(c){
        c.classList.toggle('on', String(st.months)===c.getAttribute('data-mo'));
      });
    }
    Array.prototype.forEach.call(panel.querySelectorAll('.fc-chip'), function(c){
      c.addEventListener('click', function(){
        panel.querySelector('[name=months]').value = c.getAttribute('data-mo');
        recompute();
      });
    });
    // 배율 1배로 되돌리기
    panel.querySelector('.fc-mult-reset').addEventListener('click', function(){
      panel.querySelector('[name=mult]').value = '1.0';
      recompute();
    });

    // 그래프 잠금 / 잠금해제 — 해제하면 네온 점을 마우스로 끌어서 값을 고칠 수 있다
    var lockBtn = panel.querySelector('.fc-lock');
    lockBtn.addEventListener('click', function(){
      fcUnlocked = !fcUnlocked;
      lockBtn.classList.toggle('on', fcUnlocked);
      lockBtn.textContent = fcUnlocked ? '🔓 잠금해제됨 — 점을 끌어서 수정' : '🔒 그래프 잠금';
      if (fcUnlocked) showToast('네온 예측선의 점을 위아래로 끌어보세요');
      recompute();
    });

    panel.querySelector('.fc-manual-reset').addEventListener('click', function(){
      delete fcManual[p];
      showToast('직접 수정한 값을 되돌렸어요');
      recompute();
    });

    function readState(){
      var scenario = panel.querySelector('[name=mult]').value || '1.0';
      st.method = panel.querySelector('[name=method]').value;
      st.scenario = scenario;
      st.window = parseInt(panel.querySelector('[name=window]').value,10) || 4;
      st.months = parseInt(panel.querySelector('[name=months]').value,10) || 6;
      st.start = panel.querySelector('[name=start]').value || currentYM();
      st.exclude = panel.querySelector('[name=exclude]').value.trim();
      panel.querySelector('.fc-window').style.display = st.method==='trend' ? '' : 'none';
      panel.querySelector('.fc-mult-val').textContent = '×'+(parseFloat(scenario)||0).toFixed(2);
      var recoLine = panel.querySelector('.fc-reco-line');
      if (recoLine){
        var ml = {seasonal:'계절평균', trend:'최근추세'}[st.method] || st.method;
        var isDefault = (st.method==='seasonal' && Math.abs((parseFloat(scenario)||1)-1)<0.001 && st.months===8);
        recoLine.innerHTML = ml+' · ×'+(parseFloat(scenario)||0).toFixed(2)+' · '+st.months+'개월 예측'+
          (isDefault ? ' <span class="fc-reco-tag">(클로드추천)</span>' : ' <span class="fc-reco-tag">(직접설정)</span>');
      }
    }

    function recompute(){
      readState();
      syncChips();
      var hist = history[p] || {};
      var cfg = {method:st.method, scenario:st.scenario, window:st.window, months:st.months, start:st.start,
                 exclude: st.exclude ? st.exclude.split(',').map(function(x){return x.trim();}).filter(Boolean) : []};
      fcPreview = computeForecast(hist, cfg);
      // 직접 끌어서 고친 달은 계산값 대신 그 값을 쓴다
      var ov = fcManual[p] || {};
      Object.keys(ov).forEach(function(m){
        if (fcPreview.values[m]!=null) fcPreview.values[m] = ov[m];
      });
      paintSummary();
      drawChart();
    }

    // 요약줄 + 재고막대만 다시 그림 (끌고 있는 동안엔 이것만 돌려 가볍게 유지)
    function paintSummary(){
      var vals = fcPreview.months.map(function(m){ return fcPreview.values[m]; });
      var total = vals.reduce(function(a,b){return a+b;},0);
      var avg = vals.length ? Math.round(total/vals.length) : 0;

      var proj = computeStockProjection(p, fcPreview);
      var tail = '';
      if (proj){
        tail = proj.stockout
          ? ' · <b style="color:var(--series-5)">'+proj.stockout+'부터 최대 '+fmt(proj.worstShort)+'개 부족</b>'
          : ' · 기간 말 재고 <b>'+fmt(proj.rows[proj.rows.length-1].close)+'</b>개';
      }
      var nMan = Object.keys(fcManual[p]||{}).length;
      var manTag = nMan ? ' · <b style="color:var(--fc-neon)">직접수정 '+nMan+'개월</b>' : '';
      // 월말은 괜찮은데 월 중간에만 구멍나는 경우 — 놓치기 쉬워서 따로 알려준다
      if (proj && !proj.stockout && proj.midWorst){
        tail += ' · <b style="color:var(--series-5)">'+escapeXML(proj.midWorstAt)+
                '에 '+fmt(proj.midWorst)+'개 일시 부족</b>';
      }
      panel.querySelector('.fc-manual-reset').hidden = !nMan;
      summaryEl.innerHTML = '미리보기: '+fcPreview.months[0]+' ~ '+fcPreview.months[fcPreview.months.length-1]+
        ' · 합계 <b>'+fmt(total)+'</b>개 · 월평균 <b>'+fmt(avg)+'</b>개' + tail + manTag;
      renderStockProjection(projEl, proj);
      // 시나리오 비교 그래프는 직접설정 켰을 때만 — 평소엔 추천값 한 줄이면 충분하다
      var scEl = panel.querySelector('.fc-scenarios');
      if (scEl) scEl.innerHTML = '';
      if (scEl && fcCustomOpen) renderScenarioChart(scEl, p, st);
    }

    // 그래프에서 점을 끌었을 때 불릴 콜백을 등록
    fcOnManualLive = paintSummary;
    fcOnManual = function(){ paintSummary(); drawChart(); };

    // 타이핑 중엔 살짝 묶어서 다시 그린다 (한 글자마다 큰 차트를 새로 그리지 않도록)
    var reTimer = null;
    function recomputeSoon(){ clearTimeout(reTimer); reTimer = setTimeout(recompute, 140); }
    Array.prototype.forEach.call(panel.querySelectorAll('select,input'), function(el){
      el.addEventListener('input', recomputeSoon);
      el.addEventListener('change', recompute);
    });

    panel.querySelector('.fc-save').addEventListener('click', function(){
      recompute();
      var btn = panel.querySelector('.fc-save');
      if (!LIVE){ showToast('저장은 serve 모드에서만 돼요 (python forecast_tool.py serve)', true); return; }
      var body = {product:p, method:st.method, scenario:st.scenario, window:st.window,
                  months:st.months, start:st.start, exclude:st.exclude,
                  values: fcManual[p] || {}};   // 끌어서 고친 값도 그대로 저장
      var nMan = Object.keys(fcManual[p]||{}).length;
      var orig = btn.textContent; btn.disabled=true; btn.textContent='저장 중…';
      postJSON('/api/forecast', body).then(function(newData){
        showToast(p+' 예측 저장 완료 ('+st.start+'부터 '+st.months+'개월'+(nMan?', 직접수정 '+nMan+'개월 포함':'')+')');
        applyData(newData);
      }).catch(function(err){
        showToast(err.message || '저장에 실패했어요', true);
        btn.disabled=false; btn.textContent=orig;
      });
    });

    recompute();
  }

  function renderDetail(){
    var p = selectedProduct;
    var titleEl = document.getElementById('detailTitle');
    var subEl = document.getElementById('detailSub');
    var chartEl = document.getElementById('detailChart');
    var noteEl = document.getElementById('detailLegendNote');
    var tableWrap = document.getElementById('detailTableWrap');
    var compareSection = document.getElementById('compareSection');
    var inventorySection = document.getElementById('inventorySection');
    var inputSection = document.getElementById('inputSection');
    var editSection = document.getElementById('editSection');
    var forecastSection = document.getElementById('forecastSection');
    if (!p){ renderStockNow(null); titleEl.textContent='상품 상세'; subEl.textContent=''; chartEl.innerHTML=''; noteEl.textContent=''; tableWrap.innerHTML=''; compareSection.innerHTML=''; inventorySection.innerHTML=''; inputSection.innerHTML=''; editSection.innerHTML=''; forecastSection.innerHTML=''; return; }

    titleEl.textContent = p + ' 상세';
    subEl.textContent = brandOf(p) + (meta[p]&&meta[p].unit ? ' · 단위: '+meta[p].unit : '');
    renderStockNow(p);

    inputSection.innerHTML = '';
    renderEditTable(editSection, p);

    var invEntries = inventoryData[p] || [];
    if (invEntries.length){
      var latestInv = invEntries[invEntries.length-1];
      subEl.textContent += ' · 현재 재고: ' + fmt(latestInv.qty) + '개 (' + latestInv.month + ' 기준)' +
        (latestInv.wip ? ' · 작업중 '+fmt(latestInv.wip)+'개 포함, 예측엔 미반영' : '');
    }

    var months = HIST_MONTHS.slice();
    fcPreview = null;
    renderForecastPanel(forecastSection, p);

    tableWrap.style.display='none';
    tableWrap.innerHTML = buildHistoryTableHTML(p, months);
    document.getElementById('detailTableToggle').onclick = function(){
      tableWrap.style.display = tableWrap.style.display==='none' ? 'block':'none';
    };

    var rows = compareData[p] || [];
    compareSection.innerHTML = rows.length ? buildCompareTableHTML(rows) : '';

    // 재고 추이는 위 상세 차트에 흐린 막대로 겹쳐서 표시돼요 (별도 차트 없음)
    inventorySection.innerHTML = invEntries.length ? '' :
      '<div class="empty-note" style="margin-top:12px;">아직 재고 데이터가 없어요. 위 편집기의 <b>재고 실사</b> 칸에 현재 재고를, <b>수량</b> 칸에 출고량을 입력하면 상세 차트에 재고 막대가 겹쳐 나와요.</div>';
  }

  function renderAll(){
    renderBrandFilter();
    renderKPIs();
    renderBrandChart();
    renderBrandPie();
    renderSparkGrid();
    // 신상품 패널을 먼저 그린다 — 예측 미리보기(fcPreview) 상태를 두 화면이 같이 쓰기 때문에
    // 기본 화면인 수요예측이 마지막에 그려져야 그 값을 갖고 있게 된다.
    renderNewProducts();
    renderDetail();
    renderAttention();   // 상세를 그린 뒤라야 예측 상태가 잡혀 있다
    renderToday();       // 같은 이유로 상세 뒤에 그린다
    renderLaunch();
    renderWorklog();
  }

  // ═══════════════ ERP 재고표 → 출고량·재고 실사 갱신 ═══════════════
  var ERP_LAST=null;
  function erpStat(t, cls){
    var el=document.getElementById('erpStatus'); if(!el) return;
    el.textContent=t||''; el.className='inv-status '+(cls||'');
  }

  function erpReview(pl){
    var notes=(pl.notes||[]).map(function(n){
      return '<li><span class="lv '+(n.level||'info')+'">'+(n.level==='warn'?'확인':'참고')+'</span>'+
             '<span class="wh">'+escapeXML(n['품목']||'')+'</span>'+
             '<span class="ms">'+escapeXML(n.message||'')+'</span></li>'; }).join('');
    var noteHtml = notes ? '<div class="inv-find"><div class="inv-find-h">⚠ 확인할 점</div><ul>'+notes+'</ul></div>' : '';

    var rows=(pl.changes||[]).map(function(c){
      return '<tr><td>'+escapeXML(c['품목키'])+'</td>'+
             '<td class="num">'+fmt(c['금월출고'])+'</td>'+
             '<td class="num">'+fmt(c['홀딩'])+'</td>'+
             '<td class="num"><b>'+fmt(c['실제출고'])+'</b></td>'+
             '<td class="num">'+(c['재고실사']==null?'-':fmt(c['재고실사']))+'</td></tr>'; }).join('');

    var miss=(pl.unmatched||[]).filter(function(u){ return (u['실제출고']||0)>0; })
      .sort(function(a,b){ return (b['실제출고']||0)-(a['실제출고']||0); }).slice(0,15)
      .map(function(u){ return '<li><span class="wh">'+escapeXML(u['부품코드']||'')+'</span>'+
             '<span class="ms">'+escapeXML(u['품목'])+' (실제출고 '+fmt(u['실제출고'])+')</span></li>'; }).join('');
    var missHtml = miss ? '<div class="inv-find"><div class="inv-find-h">대시보드에 없는 품목 '+
        pl['연결안됨']+'개 — 갱신에서 빠집니다 (출고 있는 것만 추림)</div><ul>'+miss+'</ul></div>' : '';
    var igHtml = pl['제외됨'] ? '<div class="card-sub" style="margin-top:8px">제외 목록에 넣어둔 '+
        pl['제외됨']+'개 품목은 건너뛰었어요.</div>' : '';

    var m=openModal('<div class="modal inv-modal"><h3>📊 재고표 확인 — '+escapeXML(pl['월'])+' 갱신</h3>'+
      '<div class="card-sub" style="margin-bottom:10px">기준일 <b>'+escapeXML(pl['기준일'])+'</b> · '+
      'ERP '+pl['ERP행수']+'행 중 <b>'+pl['연결됨']+'개</b>를 갱신합니다. '+
      '실제 출고량 = 금월 출고수량 − 홀딩재고</div>'+
      noteHtml+
      '<div class="inv-tbl-wrap"><table class="inv-tbl"><thead><tr>'+
        '<th>품목</th><th>금월 출고</th><th>홀딩</th><th>실제 출고</th><th>재고 실사</th>'+
      '</tr></thead><tbody>'+rows+'</tbody></table></div>'+
      missHtml+
      igHtml+
      '<div class="modal-actions">'+
        '<label class="erp-opt"><input type="checkbox" id="erpStock" checked> 재고 실사도 같이 갱신</label>'+
        '<button class="cust-btn ghost" id="erpCancel">취소</button>'+
        '<button class="cust-btn" id="erpGo">'+pl['연결됨']+'개 반영</button></div></div>');

    m.querySelector('#erpCancel').addEventListener('click', function(){ closeModal(); erpStat(''); });
    m.querySelector('#erpGo').addEventListener('click', function(){
      var b=m.querySelector('#erpGo'); b.disabled=true; b.textContent='반영 중…';
      postJSON('/api/erp/stock/apply', {data:ERP_LAST.b64, filename:ERP_LAST.name,
                                        do_stock:m.querySelector('#erpStock').checked})
        .then(function(r){
          closeModal();
          var sb=r.supabase||{};
          var sbText=sb.ok ? ' · Supabase 저장 완료' : ' · '+(sb.message||'Supabase 저장 건너뜀');
          erpStat(r['월']+' 갱신 완료 — 출고 '+r['출고반영']+'건, 재고 실사 '+r['재고실사반영']+'건'+
                  (r['입고건너뜀'] ? ', 직접 정한 입고 '+r['입고건너뜀']+'건은 그대로 둠' : '')+
                  sbText+' (되돌릴 백업 있음)', sb.ok?'ok':'warn');
          showToast(sb.ok?'출고량·재고·Supabase 갱신 완료':'로컬 갱신 완료 · Supabase 상태를 확인하세요', !sb.ok);
          return fetch('/api/data').then(function(x){return x.json();}).then(applyData);
        })
        .catch(function(e){ b.disabled=false; b.textContent='반영'; showToast(e.message||'반영 실패', true); });
    });
  }

  function erpHandle(file){
    if(!file) return;
    ERP_LAST=null;
    erpStat('"'+file.name+'" 읽는 중…');
    var fr=new FileReader();
    fr.onload=function(){
      ERP_LAST={b64:String(fr.result).split(',')[1]||'', name:file.name};
      postJSON('/api/erp/stock/plan', {data:ERP_LAST.b64, filename:file.name})
        .then(function(pl){
          erpStat(pl['월']+' 기준 '+pl['연결됨']+'개 품목을 갱신할 수 있어요. 확인 화면에서 봐주세요.',
                  pl['연결됨']?'ok':'warn');
          erpReview(pl);
        })
        .catch(function(e){ erpStat('읽지 못했어요: '+(e.message||''), 'warn'); });
    };
    fr.onerror=function(){ erpStat('파일을 읽지 못했어요.', 'warn'); };
    fr.readAsDataURL(file);
  }

  (function initTableauExport(){
    var btn=document.getElementById('tableauExportBtn'); if(!btn) return;
    var msg=document.getElementById('tableauExportMsg');
    btn.addEventListener('click', function(){
      btn.disabled=true; btn.textContent='내보내는 중…';
      postJSON('/api/tableau/export', {}).then(function(r){
        if(msg) msg.innerHTML='✅ '+r.rows+'행 내보냄 · <a href="'+r.sheet_url+'" target="_blank" rel="noopener">구글시트 열기</a>';
        showToast('태블로용 구글시트로 내보냈어요 ('+r.rows+'행)');
      }).catch(function(e){
        if(msg) msg.textContent='⚠ '+(e.message||'내보내기 실패');
        showToast(e.message||'내보내기 실패', true);
      }).then(function(){ btn.disabled=false; btn.textContent='📊 태블로로 내보내기(구글시트)'; });
    });

    var xlBtn=document.getElementById('tableauExportExcelBtn');
    if(xlBtn) xlBtn.addEventListener('click', function(){
      xlBtn.disabled=true; xlBtn.textContent='내보내는 중…';
      postJSON('/api/tableau/export-excel', {}).then(function(r){
        if(msg) msg.textContent='✅ '+r.rows+'행을 바탕화면 엑셀 파일로 저장했어요.';
        showToast('바탕화면에 엑셀로 내보냈어요 ('+r.rows+'행)');
      }).catch(function(e){
        if(msg) msg.textContent='⚠ '+(e.message||'내보내기 실패');
        showToast(e.message||'내보내기 실패', true);
      }).then(function(){ xlBtn.disabled=false; xlBtn.textContent='📊 태블로로 내보내기(엑셀)'; });
    });

    var poMsg=document.getElementById('poExportMsg');
    var poBtn=document.getElementById('poExportBtn');
    if(poBtn) poBtn.addEventListener('click', function(){
      poBtn.disabled=true; poBtn.textContent='내보내는 중…';
      postJSON('/api/tableau/export-po', {}).then(function(r){
        if(poMsg) poMsg.innerHTML='✅ '+r.rows+'개 상품 내보냄 · <a href="'+r.sheet_url+'" target="_blank" rel="noopener">구글시트 열기</a>';
        showToast('구매발주표를 구글시트로 내보냈어요 ('+r.rows+'개 상품)');
      }).catch(function(e){
        if(poMsg) poMsg.textContent='⚠ '+(e.message||'내보내기 실패');
        showToast(e.message||'내보내기 실패', true);
      }).then(function(){ poBtn.disabled=false; poBtn.textContent='📦 구매발주 내보내기(구글시트)'; });
    });

    var poXlBtn=document.getElementById('poExportExcelBtn');
    if(poXlBtn) poXlBtn.addEventListener('click', function(){
      poXlBtn.disabled=true; poXlBtn.textContent='내보내는 중…';
      postJSON('/api/tableau/export-po-excel', {}).then(function(r){
        if(poMsg) poMsg.textContent='✅ '+r.rows+'개 상품을 바탕화면 엑셀 파일로 저장했어요.';
        showToast('바탕화면에 구매발주표를 엑셀로 내보냈어요 ('+r.rows+'개 상품)');
      }).catch(function(e){
        if(poMsg) poMsg.textContent='⚠ '+(e.message||'내보내기 실패');
        showToast(e.message||'내보내기 실패', true);
      }).then(function(){ poXlBtn.disabled=false; poXlBtn.textContent='📦 구매발주 내보내기(엑셀)'; });
    });
  })();

  (function initErp(){
    var drop=document.getElementById('erpDrop'); if(!drop) return;
    var fi=document.getElementById('erpFile');
    document.getElementById('erpPick').addEventListener('click', function(e){ e.stopPropagation(); fi.click(); });
    drop.addEventListener('click', function(){ fi.click(); });
    fi.addEventListener('change', function(){ erpHandle(fi.files[0]); fi.value=''; });
    ['dragenter','dragover'].forEach(function(ev){
      drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add('over'); }); });
    ['dragleave','drop'].forEach(function(ev){
      drop.addEventListener(ev, function(e){ e.preventDefault();
        if(ev==='dragleave' && drop.contains(e.relatedTarget)) return;
        drop.classList.remove('over'); }); });
    drop.addEventListener('drop', function(e){
      e.preventDefault(); drop.classList.remove('over');
      if(e.dataTransfer && e.dataTransfer.files) erpHandle(e.dataTransfer.files[0]);
    });
  })();

  // ═══════════════ 인보이스 PDF → 주문 등록 ═══════════════
  function invStat(t, cls){
    var el=document.getElementById('invStatus'); if(!el) return;
    el.textContent=t||''; el.className='inv-status '+(cls||'');
  }
  var INV_COLS=[['품명','nm'],['병입수',''],['카톤수',''],['총수량',''],['파렛트별박스수',''],
                ['개당가격(EUR)',''],['소비기한',''],['산도(%)',''],['배치넘버',''],
                ['넷중량(KG)',''],['총중량(KG)','']];

  function invReview(d){
    var chk=(d.checks||[]).map(function(c){
      return '<span class="'+(c.일치?'y':'n')+'">'+escapeXML(c.항목)+' '+
             (c.일치?'일치':(fmt(c.계산)+' ≠ '+fmt(c.문서)))+'</span>'; }).join('');
    var warn=(d.warnings||[]).length
      ? '<div class="inv-warn">⚠ '+d.warnings.map(escapeXML).join('<br>')+'</div>' : '';

    // 정합성 검증 결과 — 심각한 것부터
    var order={error:0, warn:1, info:2};
    var finds=(d.findings||[]).slice().sort(function(a,b){
      return (order[a.level]==null?3:order[a.level])-(order[b.level]==null?3:order[b.level]); });
    var lvName={error:'오류', warn:'확인', info:'참고'};
    var findHtml='';
    if(finds.length){
      var nErr=finds.filter(function(f){return f.level==='error';}).length;
      var nWarn=finds.filter(function(f){return f.level==='warn';}).length;
      findHtml='<div class="inv-find"><div class="inv-find-h">🔎 정합성 검증 — '+
        finds.length+'건 (오류 '+nErr+' · 확인 '+nWarn+')</div><ul>'+
        finds.map(function(f){
          var lv=f.level||'info';
          return '<li><span class="lv '+lv+'">'+(lvName[lv]||lv)+'</span>'+
                 '<span class="wh">'+escapeXML(f.where||'')+'</span>'+
                 '<span class="ms">'+escapeXML(f.message||'')+'</span></li>';
        }).join('')+'</ul></div>';
    } else if(d.source){
      findHtml='<div class="inv-find"><div class="inv-find-h">🔎 정합성 검증 — 걸리는 곳이 없어요.</div></div>';
    }
    var srcBadge = d.source==='ai' ? '<span class="inv-src ai">클로드가 읽음</span>'
                 : (d.source==='regex' ? '<span class="inv-src regex">자동 인식 (무료)</span>' : '');

    // 이번에 쓴 토큰 — 캐시로 아낀 양도 같이 보여준다
    var u=d.usage||{}, usageHtml='';
    if(u.input!=null){
      var saved=(u.cache_read||0);
      usageHtml='<span class="inv-tok">'+escapeXML(u.mode||'')+' · 입력 '+fmt(u.input+saved+(u.cache_write||0))+
        ' · 출력 '+fmt(u.output||0)+(saved?(' · 캐시로 아낌 '+fmt(saved)):'')+'</span>';
    }
    var rows=(d.items||[]).map(function(it,i){
      return '<tr data-i="'+i+'">'+INV_COLS.map(function(c){
        var v=it[c[0]]; v=(v==null?'':v);
        return '<td><input class="'+c[1]+'" data-k="'+escapeXML(c[0])+'" value="'+escapeXML(String(v))+'"></td>';
      }).join('')+'<td><button class="rm" title="이 줄 빼기">✕</button></td></tr>';
    }).join('');

    var m=openModal('<div class="modal inv-modal"><h3>📄 인보이스 확인 — 저장 전 검토'+srcBadge+'</h3>'+
      '<div class="card-sub" style="margin-bottom:10px">PDF에서 읽은 값입니다. <b>틀린 곳은 바로 고쳐서</b> 저장하세요. '+
      '인보이스 '+escapeXML(d.invoice_no||'-')+' · 컨테이너 '+escapeXML(d.container||'-')+'</div>'+
      warn+
      findHtml+
      '<div class="inv-chk">'+chk+'</div>'+
      '<div class="inv-head">'+
        '<div class="inv-f"><label>시트 탭</label><input id="ivTab" value="'+escapeXML(d.sheet_tab||'')+'"></div>'+
        '<div class="inv-f"><label>주문명</label><input id="ivName" value="'+escapeXML(d.order_name||'')+'"></div>'+
        '<div class="inv-f"><label>BL번호</label><input id="ivBl" value="'+escapeXML(d.bl||'')+'"></div>'+
        '<div class="inv-f"><label>선적일</label><input id="ivShip" type="date" value="'+escapeXML(d.shipped||'')+'"></div>'+
        '<div class="inv-f"><label>예상입항일</label><input id="ivEta" type="date" value="'+escapeXML(d.eta||'')+'"></div>'+
        '<div class="inv-f"><label>PI기준발주일</label><input id="ivPi" value="" placeholder="없으면 비움"></div>'+
      '</div>'+
      '<div class="inv-tbl-wrap"><table class="inv-tbl"><thead><tr>'+
        INV_COLS.map(function(c){ return '<th>'+escapeXML(c[0])+'</th>'; }).join('')+'<th></th>'+
      '</tr></thead><tbody id="ivRows">'+rows+'</tbody></table></div>'+
      '<div class="modal-actions">'+
        (d.source==='regex' ? '<button class="cust-btn ghost" id="ivAi">🤖 클로드로 다시 읽기</button>' : '')+
        usageHtml+
        '<button class="cust-btn ghost" id="ivCancel">취소</button>'+
      '<button class="cust-btn" id="ivSave">시트에 저장</button></div></div>');

    var aiBtn=m.querySelector('#ivAi');
    if(aiBtn) aiBtn.addEventListener('click', function(){
      if(!INV_LAST_B64){ showToast('파일을 다시 올려주세요.', true); return; }
      aiBtn.disabled=true; aiBtn.textContent='클로드가 읽는 중…';
      postJSON('/api/customs/invoice/parse', {data:INV_LAST_B64, force_ai:true})
        .then(function(d2){ closeModal(); invReview(d2); })
        .catch(function(e){ aiBtn.disabled=false; aiBtn.textContent='🤖 클로드로 다시 읽기';
                            showToast(e.message||'실패', true); });
    });

    m.querySelector('#ivRows').addEventListener('click', function(e){
      if(e.target.classList.contains('rm')){ e.target.closest('tr').remove(); }
    });
    m.querySelector('#ivCancel').addEventListener('click', function(){ closeModal(); invStat(''); });

    m.querySelector('#ivSave').addEventListener('click', function(){
      var name=m.querySelector('#ivName').value.trim();
      var tab=m.querySelector('#ivTab').value.trim();
      if(!name || !tab){ showToast('시트 탭과 주문명은 반드시 필요해요', true); return; }
      var items=[];
      Array.prototype.forEach.call(m.querySelectorAll('#ivRows tr'), function(tr){
        var o={};
        Array.prototype.forEach.call(tr.querySelectorAll('input'), function(inp){
          o[inp.getAttribute('data-k')]=inp.value.trim();
        });
        if(o['품명']) items.push(o);
      });
      if(!items.length){ showToast('등록할 품목이 없어요', true); return; }

      var btn=m.querySelector('#ivSave'); btn.disabled=true; btn.textContent='저장 중…';
      postJSON('/api/customs/invoice/save', {
        tab:tab, order_name:name, bl:m.querySelector('#ivBl').value.trim(),
        shipped:m.querySelector('#ivShip').value, eta:m.querySelector('#ivEta').value,
        pi:m.querySelector('#ivPi').value.trim(), items:items
      }).then(function(r){
        closeModal();
        if(r.skipped){ invStat(r.message, 'warn'); showToast(r.message, true); return; }
        invStat(r.message+' · 보드 신규 '+r.new_in_board+'건', 'ok');
        showToast(r.message);
        var sb=document.getElementById('boardSyncBtn');   // 보드도 바로 새로고침
        if(sb) sb.click();
      }).catch(function(e){
        btn.disabled=false; btn.textContent='시트에 저장';
        showToast(e.message||'저장 실패', true);
      });
    });
  }

  var INV_LAST_B64='';
  function invHandle(file){
    if(!file) return;
    if(file.name.toLowerCase().slice(-4)!=='.pdf'){ invStat('PDF 파일만 됩니다.', 'warn'); return; }
    invStat('"'+file.name+'" 읽는 중… (클로드가 읽을 때는 30초쯤 걸려요)');
    var fr=new FileReader();
    fr.onload=function(){
      INV_LAST_B64=String(fr.result).split(',')[1]||'';
      postJSON('/api/customs/invoice/parse', {data:INV_LAST_B64})
        .then(function(d){
          var n=(d.items||[]).length;
          var errs=(d.findings||[]).filter(function(f){return f.level==='error';}).length;
          var how=(d.source==='ai' ? '클로드가 읽었어요' : '자동으로 읽었어요');
          if(errs) invStat(how+' — 품목 '+n+'줄, 정합성 오류 '+errs+'건. 확인 화면에서 봐주세요.', 'warn');
          else invStat(how+' — 품목 '+n+'줄. 확인 화면에서 검토해주세요.', n?'ok':'warn');
          invReview(d);
        })
        .catch(function(e){ invStat('읽지 못했어요: '+(e.message||''), 'warn'); });
    };
    fr.onerror=function(){ invStat('파일을 읽지 못했어요.', 'warn'); };
    fr.readAsDataURL(file);
  }

  (function initInvoice(){
    var drop=document.getElementById('invDrop'); if(!drop) return;
    var fi=document.getElementById('invFile');
    document.getElementById('invPick').addEventListener('click', function(e){ e.stopPropagation(); fi.click(); });
    drop.addEventListener('click', function(){ fi.click(); });
    fi.addEventListener('change', function(){ invHandle(fi.files[0]); fi.value=''; });
    ['dragenter','dragover'].forEach(function(ev){
      drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add('over'); }); });
    ['dragleave','drop'].forEach(function(ev){
      drop.addEventListener(ev, function(e){ e.preventDefault();
        if(ev==='dragleave' && drop.contains(e.relatedTarget)) return;
        drop.classList.remove('over'); }); });
    drop.addEventListener('drop', function(e){
      e.preventDefault(); drop.classList.remove('over');
      if(e.dataTransfer && e.dataTransfer.files) invHandle(e.dataTransfer.files[0]);
    });

    // 클로드 키 — 한 번 저장해두면 계속 쓴다
    var keyMsg=document.getElementById('invKeyMsg');
    function showKeyState(s){
      if(!keyMsg) return;
      if(s && s.ai_ok){
        var label = s.ai_provider==='deepseek' ? '딥시크' : '클로드';
        keyMsg.textContent='✅ '+label+' 검증 준비됨 (지금 이걸 씁니다)';
      } else keyMsg.textContent=(s && s.ai_message) || '';
    }
    fetch('/api/customs/status').then(function(r){return r.json();}).then(showKeyState).catch(function(){});
    var kb=document.getElementById('invKeySave');
    if(kb) kb.addEventListener('click', function(){
      var el=document.getElementById('invKey');
      var v=el.value.trim();
      if(!v){ showToast('키를 넣어주세요.', true); return; }
      kb.disabled=true; kb.textContent='저장 중…';
      postJSON('/api/customs/invoice/aikey', {key:v}).then(function(r){
        el.value='';
        if(keyMsg) keyMsg.textContent=(r.ok?'✅ ':'⚠ ')+r.message;
        showToast(r.ok?'클로드 키 저장 완료':r.message, !r.ok);
      }).catch(function(e){ showToast(e.message||'저장 실패', true); })
        .then(function(){ kb.disabled=false; kb.textContent='저장'; });
    });

    // 딥시크 키 — 두 키가 다 있으면 딥시크를 먼저 쓴다 (invoice_ai._provider 참고)
    var dsKeyMsg=document.getElementById('invDsKeyMsg');
    var dsKb=document.getElementById('invDsKeySave');
    if(dsKb) dsKb.addEventListener('click', function(){
      var el=document.getElementById('invDsKey');
      var v=el.value.trim();
      if(!v){ showToast('키를 넣어주세요.', true); return; }
      dsKb.disabled=true; dsKb.textContent='저장 중…';
      postJSON('/api/customs/invoice/dskey', {key:v}).then(function(r){
        el.value='';
        if(dsKeyMsg) dsKeyMsg.textContent=(r.ok?'✅ ':'⚠ ')+r.message;
        showToast(r.ok?'딥시크 키 저장 완료':r.message, !r.ok);
        fetch('/api/customs/status').then(function(rr){return rr.json();}).then(showKeyState).catch(function(){});
      }).catch(function(e){ showToast(e.message||'저장 실패', true); })
        .then(function(){ dsKb.disabled=false; dsKb.textContent='저장'; });
    });
  })();

  // 신상품관리 — 새 제품 추가 버튼
  (function(){
    var b = document.getElementById('npAdd');
    if (b) b.addEventListener('click', npAddProduct);
  })();

  // 기준월 드롭다운 → 원그래프 다시 그리기 (직접 고르면 그 달로 고정)
  document.getElementById('pieMonth').addEventListener('change', function(){
    pieMonth = this.value;
    pieMonthPinned = true;
    renderBrandPie();
  });

  // 창을 계속 켜둬도 날짜가 바뀌면 기준월이 저절로 넘어가게 (매달 1일 대응)
  (function watchMonthRollover(){
    var seen = prevYM();
    setInterval(function(){
      var now = prevYM();
      if (now !== seen){ seen = now; renderBrandPie(); }
    }, 60*1000);
  })();

  // ═══════════════ 업무일지 ═══════════════
  var wlDate = null, wlSaveTimer = null;

  function wlData(){ return (DATA && DATA.worklog) || {days:{}}; }
  function wlDay(d){ return (wlData().days||{})[d] || {text:'', files:[]}; }
  function wlFmt(n){
    if (n==null) return '';
    if (n<1024) return n+' B';
    if (n<1048576) return (n/1024).toFixed(0)+' KB';
    return (n/1048576).toFixed(1)+' MB';
  }
  function wlDayLabel(d){
    var dt=dnum(d); if(!dt) return d;
    var w=['일','월','화','수','목','금','토'][dt.getDay()];
    var n=dayDiff(todayD(), dt);
    var rel = n===0?'오늘':(n===1?'어제':(n>0?n+'일 전':''));
    return d+'('+w+')'+(rel?' · '+rel:'');
  }

  function renderWorklog(){
    var host=document.getElementById('wlDate'); if(!host) return;
    if(!wlDate) wlDate = ymd(todayD());
    host.value = wlDate;
    document.getElementById('wlTitle').textContent = wlDayLabel(wlDate)+' 기록';
    var day = wlDay(wlDate);
    var ta = document.getElementById('wlText');
    if (document.activeElement !== ta) ta.value = day.text || '';
    document.getElementById('wlStatus').textContent =
      day.updated ? '마지막 저장 '+day.updated : '';

    // 첨부 파일
    var files = day.files||[];
    document.getElementById('wlFileCount').textContent = files.length? '('+files.length+'개)' : '';
    document.getElementById('wlFiles').innerHTML = files.length
      ? '<ul class="wl-flist">'+files.map(function(f){
          return '<li><span class="fn">'+escapeXML(f.name)+'</span>'+
            '<span class="fs">'+wlFmt(f.size)+'</span>'+
            '<span class="fs">'+escapeXML(f.at||'')+'</span>'+
            (f.url?'<a class="fo" href="'+escapeXML(f.url)+'" target="_blank" rel="noopener">열기 ↗</a>':'')+
            '<button class="fx" data-fid="'+escapeXML(f.id||'')+'" title="목록·드라이브에서 삭제">×</button></li>';
        }).join('')+'</ul>'+
        '<div class="wl-up">드라이브 <b>업무일지 / '+escapeXML(wlDate)+'</b> 폴더에 저장돼 있어요 '+
        '<button type="button" class="wl-pick" id="wlFolder">폴더 열기 ↗</button></div>'
      : '<div class="empty-note" style="padding:12px 0">이 날짜에 올린 파일이 없어요</div>';

    Array.prototype.forEach.call(document.querySelectorAll('#wlFiles .fx'), function(b){
      b.addEventListener('click', function(){
        if(!confirm('이 파일을 목록과 구글드라이브에서 삭제할까요?')) return;
        postJSON('/api/worklog/delete', {date:wlDate, id:b.getAttribute('data-fid')})
          .then(function(w){ DATA.worklog=w; renderWorklog(); showToast('삭제했어요'); })
          .catch(function(e){ showToast(e.message||'실패', true); });
      });
    });
    var fb=document.getElementById('wlFolder');
    if(fb) fb.addEventListener('click', function(){
      postJSON('/api/worklog/folder', {date:wlDate})
        .then(function(r){ window.open(r.url,'_blank'); })
        .catch(function(e){ showToast(e.message||'실패', true); });
    });

    // 지난 기록
    var days = Object.keys(wlData().days||{}).filter(function(d){
      var x=wlDay(d); return (x.text&&x.text.trim()) || (x.files&&x.files.length); })
      .sort().reverse();
    document.getElementById('wlPastCount').textContent = '('+days.length+'일)';
    document.getElementById('wlPast').innerHTML = days.length
      ? '<ul class="wl-past">'+days.map(function(d){
          var x=wlDay(d), t=(x.text||'').replace(/[\r\n\t ]+/g,' ').trim();
          return '<li data-d="'+d+'"><span class="pd">'+escapeXML(wlDayLabel(d).split(' · ')[0])+'</span>'+
            '<span class="pt">'+escapeXML(t||'(내용 없음)')+'</span>'+
            '<span class="pf">'+((x.files&&x.files.length)?'📎 '+x.files.length:'')+'</span></li>';
        }).join('')+'</ul>'
      : '<div class="empty-note">아직 기록이 없어요</div>';
    Array.prototype.forEach.call(document.querySelectorAll('#wlPast li'), function(li){
      li.addEventListener('click', function(){ wlDate=li.getAttribute('data-d'); renderWorklog(); });
    });
  }

  (function initWorklog(){
    var ta=document.getElementById('wlText'); if(!ta) return;
    // 입력이 멈추면 자동 저장
    ta.addEventListener('input', function(){
      var st=document.getElementById('wlStatus');
      st.textContent='입력 중…'; st.className='wl-status';
      clearTimeout(wlSaveTimer);
      wlSaveTimer=setTimeout(function(){
        postJSON('/api/worklog/text', {date:wlDate, text:ta.value})
          .then(function(w){ DATA.worklog=w;
            var d=wlDay(wlDate);
            st.textContent='저장됨 · '+(d.updated||''); st.className='wl-status ok'; })
          .catch(function(e){ st.textContent='저장 실패: '+(e.message||''); st.className='wl-status'; });
      }, 700);
    });
    document.getElementById('wlDate').addEventListener('change', function(){
      wlDate=this.value || ymd(todayD()); renderWorklog(); });
    document.getElementById('wlToday').addEventListener('click', function(){
      wlDate=ymd(todayD()); renderWorklog(); });

    // ── 드래그앤드롭 업로드 ──
    var drop=document.getElementById('wlDrop'), fi=document.getElementById('wlFile');
    document.getElementById('wlPick').addEventListener('click', function(){ fi.click(); });
    fi.addEventListener('change', function(){ wlHandleFiles(fi.files); fi.value=''; });
    ['dragenter','dragover'].forEach(function(ev){
      drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add('over'); }); });
    ['dragleave','drop'].forEach(function(ev){
      drop.addEventListener(ev, function(e){ e.preventDefault();
        if(ev==='dragleave' && drop.contains(e.relatedTarget)) return;
        drop.classList.remove('over'); }); });
    drop.addEventListener('drop', function(e){
      e.preventDefault(); drop.classList.remove('over');
      if(e.dataTransfer && e.dataTransfer.files) wlHandleFiles(e.dataTransfer.files);
    });
    // 페이지 다른 곳에 떨궈서 파일이 열리는 것 방지
    ['dragover','drop'].forEach(function(ev){
      window.addEventListener(ev, function(e){
        if(!drop.contains(e.target)) e.preventDefault(); }); });
  })();

  // 떨어뜨린 파일 → 이름 정하기 → 업로드 (여러 개면 순서대로)
  function wlHandleFiles(fileList){
    var arr=Array.prototype.slice.call(fileList||[]);
    if(!arr.length) return;
    (function next(){
      if(!arr.length) return;
      var f=arr.shift();
      wlAskName(f, function(name){
        wlUpload(f, name, function(){ next(); });
      }, function(){ next(); });
    })();
  }

  function wlAskName(file, onOk, onCancel){
    var dot=file.name.lastIndexOf('.'), ext=dot>0?file.name.slice(dot):'';
    var base=dot>0?file.name.slice(0,dot):file.name;
    var m=openModal('<div class="modal"><h3>📎 파일 이름 정하기</h3>'+
      '<div class="card-sub" style="margin-bottom:10px">'+escapeXML(wlDate)+' 폴더에 이 이름으로 저장돼요. '+
      '원본: '+escapeXML(file.name)+' ('+wlFmt(file.size)+')</div>'+
      '<div class="modal-row"><label>파일명</label><input id="wlName" value="'+escapeXML(base)+'"></div>'+
      '<div class="modal-row"><label>확장자</label><input id="wlExt" value="'+escapeXML(ext)+'" style="max-width:110px"></div>'+
      '<div class="modal-actions"><button class="cust-btn ghost" id="wlCancel">취소</button>'+
      '<button class="cust-btn" id="wlOk">업로드</button></div></div>');
    var inp=m.querySelector('#wlName');
    setTimeout(function(){ inp.focus(); inp.select(); }, 30);
    function ok(){
      var nm=(inp.value||'').trim(); if(!nm){ showToast('파일명을 입력해주세요', true); return; }
      var ex=(m.querySelector('#wlExt').value||'').trim();
      closeModal(); onOk(nm + (ex && nm.slice(-ex.length)!==ex ? ex : ''));
    }
    m.querySelector('#wlOk').addEventListener('click', ok);
    m.querySelector('#wlCancel').addEventListener('click', function(){ closeModal(); onCancel&&onCancel(); });
    inp.addEventListener('keydown', function(e){ if(e.key==='Enter') ok(); });
  }

  function wlUpload(file, name, done){
    var st=document.getElementById('wlStatus');
    st.textContent='"'+name+'" 업로드 중…'; st.className='wl-status';
    var fr=new FileReader();
    fr.onload=function(){
      var b64=String(fr.result).split(',')[1]||'';
      postJSON('/api/worklog/upload', {date:wlDate, name:name, data:b64, mime:file.type||''})
        .then(function(r){
          DATA.worklog=r.log; renderWorklog();
          showToast('"'+name+'" 업로드 완료');
          done&&done();
        })
        .catch(function(e){
          st.textContent='업로드 실패: '+(e.message||''); st.className='wl-status';
          showToast(e.message||'업로드 실패', true); done&&done();
        });
    };
    fr.onerror=function(){ showToast('파일을 읽지 못했어요', true); done&&done(); };
    fr.readAsDataURL(file);
  }

  // ═══════════════ 신제품 출시 관리 ═══════════════
  var lnSelected = null;

  function projData(){ return (DATA && DATA.projects) || {stage_template:[], projects:[]}; }
  function projList(){ return projData().projects || []; }

  function dnum(s){ // 'YYYY-MM-DD' -> Date (없으면 null)
    if (!s || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(s)) return null;
    var p = s.split('-');
    return new Date(+p[0], +p[1]-1, +p[2]);
  }
  function todayD(){ var d=new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function dayDiff(a,b){ return Math.round((a-b)/86400000); }
  function ymd(d){
    var m=d.getMonth()+1, dd=d.getDate();
    return d.getFullYear()+'-'+(m<10?'0':'')+m+'-'+(dd<10?'0':'')+dd;
  }

  function ddayInfo(p){
    var t = dnum(p.target);
    if (!t) return {txt:'목표일 미정', cls:''};
    var n = dayDiff(t, todayD());
    if (n < 0)  return {txt:'D+'+(-n)+' 지남', cls:'late'};
    if (n === 0) return {txt:'D-DAY', cls:'warn'};
    return {txt:'D-'+n, cls: n<=14 ? 'warn' : 'ok'};
  }

  function stageProgress(p){
    var st = p.stages||[];
    var done = st.filter(function(s){ return s.done; }).length;
    return {done:done, total:st.length, pct: st.length? done/st.length*100 : 0};
  }

  function renderLaunch(){
    var grid = document.getElementById('lnGrid');
    if (!grid) return;
    var list = projList();
    document.getElementById('lnCount').textContent = '('+list.length+'건)';

    if (!list.length){
      grid.innerHTML = '<div class="empty-note">아직 프로젝트가 없어요. 오른쪽 위 <b>+ 새 프로젝트</b>로 추가하세요.</div>';
      document.getElementById('lnTimeline').innerHTML = '';
      document.getElementById('lnDetail').innerHTML = '';
      return;
    }
    if (!lnSelected || !list.some(function(p){ return p.id===lnSelected; })) lnSelected = list[0].id;

    grid.innerHTML = list.map(function(p){
      var d = ddayInfo(p), pr = stageProgress(p);
      return '<div class="ln-card'+(p.id===lnSelected?' selected':'')+'" data-id="'+escapeXML(p.id)+'">'+
        '<span class="ln-dday '+d.cls+'">'+d.txt+'</span>'+
        '<div class="ln-nm">'+escapeXML(p.name)+'</div>'+
        '<div class="ln-mk">'+escapeXML(p.maker||'제조사 미정')+(p.contact?' · '+escapeXML(p.contact):'')+'</div>'+
        '<div class="ln-prog"><i style="width:'+pr.pct.toFixed(0)+'%"></i></div>'+
        '<div class="ln-meta"><span>단계 <b>'+pr.done+'/'+pr.total+'</b></span>'+
          '<span>목표 <b>'+escapeXML(p.target||'—')+'</b></span>'+
          '<span>기록 <b>'+((p.logs||[]).length)+'</b></span></div>'+
      '</div>';
    }).join('');

    Array.prototype.forEach.call(grid.querySelectorAll('.ln-card'), function(el){
      el.addEventListener('click', function(){ lnSelected = el.getAttribute('data-id'); renderLaunch(); });
    });

    renderLaunchTimeline(list);
    renderLaunchDetail(list.filter(function(p){ return p.id===lnSelected; })[0]);
  }

  // ── 전체 타임라인 (간트) ──
  function renderLaunchTimeline(list){
    var host = document.getElementById('lnTimeline');
    var pts = [];
    list.forEach(function(p){
      (p.stages||[]).forEach(function(s){
        var a=dnum(s.start), b=dnum(s.end);
        if (a) pts.push(a); if (b) pts.push(b);
      });
      var t=dnum(p.target); if (t) pts.push(t);
    });
    pts.push(todayD());
    if (!pts.length){ host.innerHTML='<div class="empty-note">날짜가 입력된 단계가 없어요</div>'; return; }

    var min = new Date(Math.min.apply(null, pts)), max = new Date(Math.max.apply(null, pts));
    min.setDate(min.getDate()-7); max.setDate(max.getDate()+7);
    var span = Math.max(1, dayDiff(max, min));
    document.getElementById('lnTlRange').textContent = '· '+ymd(min)+' ~ '+ymd(max);

    var rowsAll = [];
    list.forEach(function(p, pi){
      rowsAll.push({type:'proj', name:p.name, p:p, pi:pi});
      (p.stages||[]).forEach(function(s){ rowsAll.push({type:'stage', s:s, p:p, pi:pi}); });
    });

    var LBL=210, RH=21, PT=26, PB=8, W=980;
    var H = PT + rowsAll.length*RH + PB;
    var plotW = W - LBL - 20;
    function X(d){ return LBL + dayDiff(d, min)/span*plotW; }

    var svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+'" style="display:block">';

    // 월 눈금
    var cur = new Date(min.getFullYear(), min.getMonth(), 1);
    while (cur <= max){
      if (cur >= min){
        var x = X(cur);
        svg += '<line x1="'+x.toFixed(1)+'" y1="'+(PT-8)+'" x2="'+x.toFixed(1)+'" y2="'+(H-PB)+'" stroke="var(--gridline)"/>';
        svg += '<text x="'+(x+3).toFixed(1)+'" y="'+(PT-12)+'" class="axis-label">'+(cur.getFullYear()+'').slice(2)+'-'+('0'+(cur.getMonth()+1)).slice(-2)+'</text>';
      }
      cur = new Date(cur.getFullYear(), cur.getMonth()+1, 1);
    }

    rowsAll.forEach(function(r, i){
      var y = PT + i*RH;
      if (r.type==='proj'){
        svg += '<text x="6" y="'+(y+14)+'" class="tl-row-label" style="font-weight:700;fill:var(--text-primary)">'+
               escapeXML(r.name.length>26?r.name.slice(0,25)+'…':r.name)+'</text>';
        var t = dnum(r.p.target);
        if (t){
          var tx = X(t);
          svg += '<line x1="'+tx.toFixed(1)+'" y1="'+y+'" x2="'+tx.toFixed(1)+'" y2="'+(y+RH*((r.p.stages||[]).length+1))+'" class="tl-target"/>';
          svg += '<text x="'+(tx+4).toFixed(1)+'" y="'+(y+12)+'" class="axis-label" style="fill:var(--series-3)">목표 '+escapeXML(r.p.target)+'</text>';
        }
        return;
      }
      var s = r.s;
      svg += '<text x="18" y="'+(y+14)+'" class="tl-row-label">'+
             escapeXML(s.name.length>24?s.name.slice(0,23)+'…':s.name)+'</text>';
      var a = dnum(s.start), b = dnum(s.end) || a;
      if (!a && !b) return;
      if (!a) a = b;
      var x1 = X(a), x2 = Math.max(X(b), x1+7);
      var col = s.done ? colorOfBrand('', r.pi) : 'var(--series-3)';
      svg += '<rect class="tl-bar" x="'+x1.toFixed(1)+'" y="'+(y+4)+'" width="'+(x2-x1).toFixed(1)+
             '" height="'+(RH-9)+'" fill="'+col+'" opacity="'+(s.done?0.95:0.5)+'">'+
             '<title>'+escapeXML(r.p.name+' · '+s.name+'\n'+(s.start||'?')+' ~ '+(s.end||'진행중')+(s.done?' (완료)':''))+'</title></rect>';
    });

    var tx0 = X(todayD());
    svg += '<line x1="'+tx0.toFixed(1)+'" y1="'+(PT-8)+'" x2="'+tx0.toFixed(1)+'" y2="'+(H-PB)+'" class="tl-today"/>';
    svg += '<text x="'+(tx0+4).toFixed(1)+'" y="'+(PT-2)+'" class="axis-label" style="fill:var(--div-neg)">오늘</text>';
    svg += '</svg>';
    host.innerHTML = '<div style="overflow-x:auto">'+svg+'</div>';
  }

  // ── 선택한 프로젝트 상세 (단계 + 일지) ──
  function renderLaunchDetail(p){
    var host = document.getElementById('lnDetail');
    if (!p){ host.innerHTML=''; return; }
    var d = ddayInfo(p), pr = stageProgress(p);

    var links = (p.links||[]).map(function(l){
      return '<a class="ln-link" href="'+escapeXML(l.url)+'" target="_blank" rel="noopener">🔗 '+escapeXML(l.label||l.url)+'</a>';
    }).join('');

    var stages = (p.stages||[]).map(function(s,i){
      return '<div class="ln-stage'+(s.done?' done':'')+'">'+
        '<input type="checkbox" class="st-done" data-i="'+i+'"'+(s.done?' checked':'')+'>'+
        '<span class="st-nm">'+escapeXML(s.name)+'</span>'+
        '<input type="date" class="st-start" data-i="'+i+'" value="'+escapeXML(s.start||'')+'" title="시작일">'+
        '<span style="color:var(--text-muted)">~</span>'+
        '<input type="date" class="st-end" data-i="'+i+'" value="'+escapeXML(s.end||'')+'" title="완료일">'+
      '</div>';
    }).join('');

    var logs = (p.logs||[]).map(function(lg){
      return '<li><span class="lg-d">'+escapeXML(lg.date)+'</span>'+escapeXML(lg.text)+
             '<button class="lg-x" data-d="'+escapeXML(lg.date)+'" data-t="'+escapeXML(lg.text)+'" title="삭제">×</button></li>';
    }).join('');

    host.innerHTML =
      '<div class="ln-head">'+
        '<div><h3>'+escapeXML(p.name)+' <span class="ln-dday '+d.cls+'">'+d.txt+'</span></h3>'+
          '<div class="card-sub">'+escapeXML(p.maker||'제조사 미정')+(p.contact?' · 담당 '+escapeXML(p.contact):'')+
          ' · 목표 입고 '+escapeXML(p.target||'미정')+' · 단계 '+pr.done+'/'+pr.total+'</div>'+
          (p.memo?'<div class="card-sub" style="margin-top:4px">'+escapeXML(p.memo)+'</div>':'')+
          '<div class="ln-links">'+links+'</div>'+
        '</div>'+
        '<div style="display:flex;gap:6px"><button class="cust-btn ghost" id="lnEdit">정보 수정</button>'+
        '<button class="edit-del-btn" id="lnDel">삭제</button></div>'+
      '</div>'+
      '<div class="ln-stages"><h3 style="font-size:13px;margin:14px 0 4px;">진행 단계</h3>'+
        '<div class="card-sub" style="margin-bottom:6px">체크하면 완료로 기록되고 타임라인 막대가 진해져요. 날짜를 넣으면 타임라인에 표시됩니다.</div>'+
        stages+'</div>'+
      '<h3 style="font-size:13px;margin:18px 0 4px;">일지 <span class="card-sub">('+((p.logs||[]).length)+'건)</span></h3>'+
      '<div class="card-sub">그날 있었던 일을 남겨두면 나중에 이 프로젝트가 어떻게 흘러갔는지 그대로 볼 수 있어요.</div>'+
      '<div class="ln-logform">'+
        '<input type="date" id="lnLogDate" value="'+ymd(todayD())+'">'+
        '<input type="text" id="lnLogText" placeholder="오늘 있었던 일 (예: 라벨 확정본 수령, 단가 재협의 요청)">'+
        '<button class="cust-btn" id="lnLogAdd">기록 추가</button>'+
      '</div>'+
      (logs ? '<ul class="ln-logs">'+logs+'</ul>' : '<div class="empty-note">아직 기록이 없어요</div>');

    // 단계 체크/날짜 변경 -> 저장
    function saveStages(){
      var proj = JSON.parse(JSON.stringify(p));
      Array.prototype.forEach.call(host.querySelectorAll('.st-done'), function(c){
        proj.stages[+c.getAttribute('data-i')].done = c.checked; });
      Array.prototype.forEach.call(host.querySelectorAll('.st-start'), function(c){
        proj.stages[+c.getAttribute('data-i')].start = c.value; });
      Array.prototype.forEach.call(host.querySelectorAll('.st-end'), function(c){
        proj.stages[+c.getAttribute('data-i')].end = c.value; });
      postJSON('/api/project/save', {project: proj}).then(applyData)
        .catch(function(e){ showToast(e.message||'저장 실패', true); });
    }
    Array.prototype.forEach.call(host.querySelectorAll('.st-done,.st-start,.st-end'), function(el){
      el.addEventListener('change', saveStages);
    });

    // 일지 추가
    function addLog(){
      var txt = host.querySelector('#lnLogText').value.trim();
      if (!txt){ showToast('내용을 입력해주세요', true); return; }
      postJSON('/api/project/log', {id:p.id, date:host.querySelector('#lnLogDate').value, text:txt})
        .then(function(nd){ applyData(nd); showToast('기록을 남겼어요'); })
        .catch(function(e){ showToast(e.message||'저장 실패', true); });
    }
    host.querySelector('#lnLogAdd').addEventListener('click', addLog);
    host.querySelector('#lnLogText').addEventListener('keydown', function(e){ if(e.key==='Enter') addLog(); });

    Array.prototype.forEach.call(host.querySelectorAll('.lg-x'), function(b){
      b.addEventListener('click', function(){
        postJSON('/api/project/log/delete', {id:p.id, date:b.getAttribute('data-d'), text:b.getAttribute('data-t')})
          .then(applyData).catch(function(e){ showToast(e.message||'실패', true); });
      });
    });

    host.querySelector('#lnEdit').addEventListener('click', function(){ showProjectForm(p); });
    host.querySelector('#lnDel').addEventListener('click', function(){
      if (!confirm('"'+p.name+'" 프로젝트를 삭제할까요? 일지도 함께 지워지고 되돌릴 수 없어요.')) return;
      postJSON('/api/project/delete', {id:p.id}).then(function(nd){
        lnSelected=null; applyData(nd); showToast('삭제했어요');
      }).catch(function(e){ showToast(e.message||'실패', true); });
    });
  }

  // ── 프로젝트 추가/수정 폼 ──
  function showProjectForm(p){
    p = p || {};
    var isNew = !p.id;
    var links = (p.links||[]);
    var m = openModal('<div class="modal"><h3>'+(isNew?'새 프로젝트':'프로젝트 정보 수정')+'</h3>'+
      '<div class="modal-row"><label>제품명</label><input id="pfName" value="'+escapeXML(p.name||'')+'" placeholder="예: 포스티보나 아보카도오일 250ml"></div>'+
      '<div class="modal-row"><label>제조사</label><input id="pfMaker" value="'+escapeXML(p.maker||'')+'"></div>'+
      '<div class="modal-row"><label>담당자</label><input id="pfContact" value="'+escapeXML(p.contact||'')+'"></div>'+
      '<div class="modal-row"><label>목표 입고일</label><input id="pfTarget" type="date" value="'+escapeXML(p.target||'')+'"></div>'+
      '<div class="modal-row"><label>메모</label><input id="pfMemo" value="'+escapeXML(p.memo||'')+'"></div>'+
      '<div class="modal-row"><label>링크1 이름</label><input id="pfL1n" value="'+escapeXML((links[0]||{}).label||'')+'" placeholder="구글드라이브 / 노션 등"></div>'+
      '<div class="modal-row"><label>링크1 주소</label><input id="pfL1u" value="'+escapeXML((links[0]||{}).url||'')+'" placeholder="https://..."></div>'+
      '<div class="modal-row"><label>링크2 이름</label><input id="pfL2n" value="'+escapeXML((links[1]||{}).label||'')+'"></div>'+
      '<div class="modal-row"><label>링크2 주소</label><input id="pfL2u" value="'+escapeXML((links[1]||{}).url||'')+'"></div>'+
      '<div class="modal-actions"><button class="cust-btn ghost" id="pfCancel">취소</button>'+
      '<button class="cust-btn" id="pfSave">저장</button></div></div>');

    m.querySelector('#pfCancel').addEventListener('click', closeModal);
    m.querySelector('#pfSave').addEventListener('click', function(){
      var name = m.querySelector('#pfName').value.trim();
      if (!name){ showToast('제품명을 입력해주세요', true); return; }
      var ls = [];
      [['#pfL1n','#pfL1u'],['#pfL2n','#pfL2u']].forEach(function(pair){
        var u = m.querySelector(pair[1]).value.trim();
        if (u) ls.push({label: m.querySelector(pair[0]).value.trim() || u, url: u});
      });
      var proj = {
        id: p.id || '', name: name,
        maker: m.querySelector('#pfMaker').value.trim(),
        contact: m.querySelector('#pfContact').value.trim(),
        target: m.querySelector('#pfTarget').value,
        memo: m.querySelector('#pfMemo').value.trim(),
        links: ls, status: p.status || '진행중'
      };
      if (!isNew){ proj.stages = p.stages; proj.logs = p.logs; }
      var btn=this; btn.disabled=true; btn.textContent='저장 중…';
      postJSON('/api/project/save', {project: proj}).then(function(nd){
        closeModal(); applyData(nd); showToast(isNew?'프로젝트를 추가했어요':'저장했어요');
      }).catch(function(e){ showToast(e.message||'저장 실패', true); btn.disabled=false; btn.textContent='저장'; });
    });
  }

  (function(){
    var b = document.getElementById('lnNewBtn');
    if (b) b.addEventListener('click', function(){ showProjectForm(null); });
  })();

  // ═══════════════ Supabase Database Schema ═══════════════
  var SCHEMA_DATA=null, SCHEMA_SELECTED='', SCHEMA_LOADING=false;

  function schemaKey(t){ return (t.schema||'public')+'.'+t.name; }
  function schemaByKey(key){
    return ((SCHEMA_DATA&&SCHEMA_DATA.tables)||[]).filter(function(t){return schemaKey(t)===key;})[0]||null;
  }
  function schemaBadge(text, cls){ return '<span class="schema-badge '+(cls||'')+'">'+escapeXML(text)+'</span>'; }

  function schemaLoad(){
    if(SCHEMA_LOADING) return;
    SCHEMA_LOADING=true;
    var status=document.getElementById('schemaStatus');
    var btn=document.getElementById('schemaRefresh');
    if(status){ status.textContent='Supabase의 최신 스키마를 읽는 중…'; status.className='schema-status loading'; }
    if(btn){ btn.disabled=true; btn.textContent='불러오는 중…'; }
    fetch('/api/database/schema').then(function(r){
      return r.json().then(function(j){ if(!r.ok||j.error) throw new Error(j.error||('요청 실패 '+r.status)); return j; });
    }).then(function(data){
      SCHEMA_DATA=data;
      var tables=data.tables||[];
      if(!SCHEMA_SELECTED || !schemaByKey(SCHEMA_SELECTED)) SCHEMA_SELECTED=tables.length?schemaKey(tables[0]):'';
      if(status){
        var schemas={}; tables.forEach(function(t){schemas[t.schema||'public']=1;});
        status.textContent=(data.generated_at?'조회 '+String(data.generated_at).replace('T',' ').slice(0,19)+' · ':'')+
          Object.keys(schemas).length+'개 스키마 · '+tables.length+'개 테이블/뷰'+
          (data.warning?' · '+data.warning:'');
        status.className='schema-status '+(data.warning?'warn':'ok');
      }
      schemaRender();
    }).catch(function(e){
      SCHEMA_DATA={tables:[]};
      if(status){ status.textContent='스키마를 불러오지 못했습니다: '+(e.message||e); status.className='schema-status warn'; }
      schemaRender();
    }).then(function(){
      SCHEMA_LOADING=false;
      if(btn){ btn.disabled=false; btn.textContent='↻ 스키마 새로고침'; }
    });
  }

  function schemaRender(){
    var graph=document.getElementById('schemaGraph'), detail=document.getElementById('schemaDetail');
    if(!graph||!detail) return;
    var all=(SCHEMA_DATA&&SCHEMA_DATA.tables)||[];
    var query=((document.getElementById('schemaSearch')||{}).value||'').trim().toLowerCase();
    var parentChildren={};
    all.forEach(function(t){
      if(t.parent_name){
        var pk=(t.parent_schema||t.schema||'public')+'.'+t.parent_name;
        (parentChildren[pk]||(parentChildren[pk]=[])).push(t);
      }
    });
    var visible=all.filter(function(t){
      if(t.parent_name) return false;
      if(!query) return true;
      var hay=[t.schema,t.name].concat((t.columns||[]).map(function(c){return c.name;}));
      var kids=parentChildren[schemaKey(t)]||[];
      hay=hay.concat(kids.map(function(k){return k.name;}));
      return hay.join(' ').toLowerCase().indexOf(query)>=0;
    });
    if(!visible.length){
      graph.innerHTML='<div class="empty-note">표시할 테이블이 없습니다. 연결 설정이나 검색어를 확인하세요.</div>';
    } else {
      var cards=visible.map(function(t){
        var key=schemaKey(t), fks=t.foreign_keys||[], kids=parentChildren[key]||[];
        var pk=(t.primary_key||[]).length;
        var preview=(t.columns||[]).slice(0,6).map(function(c){
          return '<li>'+escapeXML(c.name)+' <span>'+escapeXML(c.data_type||'')+'</span></li>';
        }).join('');
        var childHtml=kids.length?'<div class="schema-children"><b>하위 테이블 '+kids.length+'개</b>'+kids.map(function(k){
          return '<button type="button" data-schema-child="'+escapeXML(schemaKey(k))+'">'+escapeXML(k.name)+'</button>';
        }).join('')+'</div>':'';
        return '<article class="schema-table-card'+(key===SCHEMA_SELECTED?' selected':'')+'" data-schema-key="'+escapeXML(key)+'">'+
          '<div class="schema-table-head"><div><small>'+escapeXML(t.schema||'public')+'</small><h3>'+escapeXML(t.name)+'</h3></div>'+
          schemaBadge(t.type||'table',t.type==='view'?'view':'')+'</div>'+
          '<div class="schema-table-meta">'+(t.columns||[]).length+' columns · '+pk+' PK · '+fks.length+' FK</div>'+
          '<ul>'+preview+'</ul>'+((t.columns||[]).length>6?'<div class="schema-more">+'+((t.columns||[]).length-6)+' more</div>':'')+
          childHtml+'</article>';
      }).join('');
      graph.innerHTML='<div class="schema-graph-inner"><svg class="schema-lines" aria-hidden="true"><defs><marker id="schemaArrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"></path></marker></defs></svg><div class="schema-grid">'+cards+'</div></div>';
      Array.prototype.forEach.call(graph.querySelectorAll('.schema-table-card'),function(card){
        card.addEventListener('click',function(e){
          if(e.target&&e.target.getAttribute('data-schema-child')) return;
          SCHEMA_SELECTED=card.getAttribute('data-schema-key'); schemaRender();
        });
      });
      Array.prototype.forEach.call(graph.querySelectorAll('[data-schema-child]'),function(button){
        button.addEventListener('click',function(e){ e.stopPropagation(); SCHEMA_SELECTED=button.getAttribute('data-schema-child'); schemaRender(); });
      });
      setTimeout(schemaDrawLines,20);
    }
    schemaRenderDetail(parentChildren);
  }

  function schemaDrawLines(){
    var graph=document.getElementById('schemaGraph'); if(!graph) return;
    var wrap=graph.querySelector('.schema-graph-inner'), svg=graph.querySelector('.schema-lines');
    if(!wrap||!svg||!SCHEMA_DATA) return;
    var box=wrap.getBoundingClientRect();
    svg.setAttribute('width',Math.max(wrap.scrollWidth,wrap.clientWidth));
    svg.setAttribute('height',Math.max(wrap.scrollHeight,wrap.clientHeight));
    var defs=svg.querySelector('defs').outerHTML, paths=[];
    (SCHEMA_DATA.tables||[]).forEach(function(t){
      if(t.parent_name) return;
      var from=graph.querySelector('[data-schema-key="'+CSS.escape(schemaKey(t))+'"]'); if(!from) return;
      (t.foreign_keys||[]).forEach(function(fk){
        var target=(fk.foreign_schema||'public')+'.'+fk.foreign_table;
        var to=graph.querySelector('[data-schema-key="'+CSS.escape(target)+'"]'); if(!to||to===from) return;
        var a=from.getBoundingClientRect(), b=to.getBoundingClientRect();
        var x1=a.left-box.left+a.width/2, y1=a.top-box.top+a.height/2;
        var x2=b.left-box.left+b.width/2, y2=b.top-box.top+b.height/2;
        var bend=Math.max(40,Math.abs(x2-x1)*.45);
        var d='M'+x1+','+y1+' C'+(x1+bend)+','+y1+' '+(x2-bend)+','+y2+' '+x2+','+y2;
        paths.push('<path d="'+d+'" marker-end="url(#schemaArrow)"><title>'+escapeXML((fk.columns||[]).join(', ')+' → '+target)+'</title></path>');
      });
    });
    svg.innerHTML=defs+paths.join('');
  }

  function schemaRenderDetail(parentChildren){
    var host=document.getElementById('schemaDetail'); if(!host) return;
    var t=schemaByKey(SCHEMA_SELECTED);
    if(!t){ host.innerHTML='<div class="empty-note">테이블을 선택하면 상세 구조가 표시됩니다.</div>'; return; }
    var pks=t.primary_key||[], fkByCol={};
    (t.foreign_keys||[]).forEach(function(f){ (f.columns||[]).forEach(function(c){fkByCol[c]=f;}); });
    var rows=(t.columns||[]).map(function(c){
      var marks=(pks.indexOf(c.name)>=0?schemaBadge('PK','pk'):'')+(fkByCol[c.name]?schemaBadge('FK','fk'):'');
      var target=fkByCol[c.name]?' → '+(fkByCol[c.name].foreign_schema||'public')+'.'+fkByCol[c.name].foreign_table+'.'+(fkByCol[c.name].foreign_columns||[]).join(','):'';
      return '<tr><td><b>'+escapeXML(c.name)+'</b>'+marks+'<div class="schema-ref">'+escapeXML(target)+'</div></td>'+
        '<td><code>'+escapeXML(c.data_type||'')+'</code></td><td>'+(c.nullable?'NULL':'필수')+'</td></tr>';
    }).join('');
    var relations=(t.foreign_keys||[]).map(function(f){
      return '<li><b>'+escapeXML((f.columns||[]).join(', '))+'</b> → '+escapeXML((f.foreign_schema||'public')+'.'+f.foreign_table+' ('+(f.foreign_columns||[]).join(', ')+')')+'</li>';
    }).join('');
    var kids=parentChildren[schemaKey(t)]||[];
    var childHtml=kids.length?'<div class="schema-detail-block"><h3>하위 테이블 / 파티션</h3><div class="schema-child-list">'+kids.map(function(k){
      return '<button type="button" data-detail-child="'+escapeXML(schemaKey(k))+'">'+escapeXML(k.name)+'</button>';
    }).join('')+'</div></div>':'';
    var parent=t.parent_name?'<div class="schema-parent">상위 테이블: <button type="button" data-detail-child="'+escapeXML((t.parent_schema||t.schema)+'.'+t.parent_name)+'">'+escapeXML(t.parent_name)+'</button></div>':'';
    host.innerHTML='<div class="schema-detail-head"><small>'+escapeXML(t.schema||'public')+'</small><h2>'+escapeXML(t.name)+'</h2>'+schemaBadge(t.type||'table')+'</div>'+parent+
      '<div class="schema-detail-block"><h3>Columns <span>'+((t.columns||[]).length)+'</span></h3><div class="schema-column-wrap"><table><thead><tr><th>이름</th><th>형식</th><th>NULL</th></tr></thead><tbody>'+rows+'</tbody></table></div></div>'+
      (relations?'<div class="schema-detail-block"><h3>Foreign keys</h3><ul class="schema-rel-list">'+relations+'</ul></div>':'')+childHtml;
    Array.prototype.forEach.call(host.querySelectorAll('[data-detail-child]'),function(button){
      button.addEventListener('click',function(){ SCHEMA_SELECTED=button.getAttribute('data-detail-child'); schemaRender(); });
    });
  }

  (function initSchema(){
    var refresh=document.getElementById('schemaRefresh'), search=document.getElementById('schemaSearch');
    if(refresh) refresh.addEventListener('click',schemaLoad);
    if(search) search.addEventListener('input',schemaRender);
    window.addEventListener('resize',function(){ if(!SCHEMA_LOADING) schemaDrawLines(); });
  })();

  // ═══════════════ 로그인 게이트 (비밀번호 1226) ═══════════════
  (function(){
    var GATE_PW = '1226';
    var gate = document.getElementById('loginGate');
    var shell = document.getElementById('appShell');
    var pw = document.getElementById('loginPw');
    var btn = document.getElementById('loginBtn');
    var err = document.getElementById('loginErr');
    function unlock(){ gate.style.display='none'; shell.hidden=false; }
    function tryLogin(){
      if (pw.value === GATE_PW){ sessionStorage.setItem('ub_auth','1'); err.hidden=true; unlock(); startFx(); }
      else { err.hidden=false; pw.value=''; pw.focus(); }
    }
    btn.addEventListener('click', tryLogin);
    pw.addEventListener('keydown', function(e){ if(e.key==='Enter') tryLogin(); });
    if (sessionStorage.getItem('ub_auth')==='1'){ unlock(); }
    else { setTimeout(function(){ pw.focus(); }, 120); }
  })();

  // ═══════════════ 뷰 전환 (수요예측 / 통관) ═══════════════
  (function(){
    var btns = document.querySelectorAll('.topnav-btn');
    Array.prototype.forEach.call(btns, function(b){
      b.addEventListener('click', function(){
        Array.prototype.forEach.call(btns, function(x){ x.classList.toggle('active', x===b); });
        var view = b.getAttribute('data-view');
        // forecast-only 스냅샷에서는 일부 뷰가 아예 없으므로 null 체크
        var vf = document.getElementById('viewForecast');
        var vn = document.getElementById('viewNewProd');
        var vl = document.getElementById('viewLaunch');
        var ve = document.getElementById('viewEcom');
        var vs = document.getElementById('viewSchema');
        var vw = document.getElementById('viewLog');
        var vc = document.getElementById('viewCustoms');
        if (vf) vf.hidden = (view!=='forecast');
        if (vn) vn.hidden = (view!=='newprod');
        if (vl) vl.hidden = (view!=='launch');
        if (ve) ve.hidden = (view!=='ecom');
        if (vs) vs.hidden = (view!=='schema');
        if (vw) vw.hidden = (view!=='wlog');
        if (vc) vc.hidden = (view!=='customs');
        if (view==='wlog') renderWorklog();
        // 예측 미리보기 상태는 화면끼리 공유하므로, 들어간 화면이 다시 잡도록 한다
        if (view==='newprod') renderNewProducts();
        if (view==='forecast') renderDetail();
        // 이커머스 화면은 처음 열 때 한 번만 iframe 을 불러온다 (초기 로딩 부담 줄이기)
        if (view==='ecom'){
          var fr = document.getElementById('ecomFrame');
          if (fr && !fr.getAttribute('src')) fr.setAttribute('src', '/ecommerce.html');
        }
        if (view==='schema') schemaLoad();
      });
    });
  })();

  // ═══════════════ 환율 + 스페인/서울 시간 위젯 ═══════════════
  var fxData = null, fxStarted = false;
  function renderFxWidget(){
    var el = document.getElementById('fxWidget');
    if (!el) return;
    var now = new Date();
    function tz(zone){ return new Intl.DateTimeFormat('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:zone}).format(now); }
    var html = '';
    html += '<div class="fx-block fx-clock"><span class="fx-k">🇪🇸 스페인(마드리드·바르셀로나)</span><span class="fx-v">'+tz('Europe/Madrid')+'</span></div>';
    html += '<div class="fx-sep"></div>';
    html += '<div class="fx-block fx-clock"><span class="fx-k">🇰🇷 서울</span><span class="fx-v">'+tz('Asia/Seoul')+'</span></div>';
    if (fxData){
      html += '<div class="fx-sep"></div>';
      html += '<div class="fx-block"><span class="fx-k">EUR/USD</span><span class="fx-v">'+fxData.eurusd.toFixed(4)+'</span></div>';
      html += '<div class="fx-block"><span class="fx-k">USD/KRW</span><span class="fx-v">₩'+Math.round(fxData.usdkrw).toLocaleString('ko-KR')+'</span></div>';
      html += '<div class="fx-block"><span class="fx-k">EUR/KRW</span><span class="fx-v">₩'+Math.round(fxData.eurkrw).toLocaleString('ko-KR')+'</span></div>';
      el.classList.remove('loading');
    } else {
      html += '<div class="fx-sep"></div><div class="fx-block"><span class="fx-k">환율</span><span class="fx-v">…</span></div>';
    }
    el.innerHTML = html;
  }
  function fetchFx(){
    fetch('https://open.er-api.com/v6/latest/EUR').then(function(r){ return r.json(); }).then(function(j){
      if (j && j.rates && j.rates.USD && j.rates.KRW){
        var eurusd=j.rates.USD, eurkrw=j.rates.KRW;
        fxData = { eurusd:eurusd, eurkrw:eurkrw, usdkrw:eurkrw/eurusd };
        renderFxWidget();
      }
    }).catch(function(){ /* 오프라인이면 시계만 표시 */ });
  }
  function startFx(){
    if (fxStarted) return; fxStarted=true;
    renderFxWidget(); fetchFx();
    setInterval(renderFxWidget, 1000);
    setInterval(fetchFx, 60000);
  }
  if (sessionStorage.getItem('ub_auth')==='1'){ startFx(); }

  // ═══════════════ 통관: 공통 상태 ═══════════════
  var customsStatus = null;
  fetch('/api/customs/status').then(function(r){return r.json();}).then(function(s){ customsStatus=s; }).catch(function(){});

  var CUST_STAGES = ['입항예정','통관진행중','통관완료'];
  var CUST_STAGE_COLORS = {'입항예정':'#64748b','통관진행중':'#ea580c','통관완료':'#16a34a'};
  var CUST_HEADER = ['BL번호','제조사','주문명','선적일','예상입항일','화물관리번호','상태','통관진행상태','통관진행사항','입항일','반출일','갱신시각','수입신고','이상사항'];
  var boardRows = [];
  function setBoardStatus(msg){
    var el = document.getElementById('boardStatus');   // 통관 탭을 감추면 없을 수 있다
    if (el) el.textContent = msg;
  }

  // ═══════════════ 통관: BL 조회 ═══════════════
  (function(){
    // 통관 탭을 감춘 경우(HIDDEN_VIEWS) 이 화면 자체가 없다 — 그냥 넘어간다
    if (!document.getElementById('custQueryBtn')) return;
    var modeRadios = document.getElementsByName('custMode');
    var yearInput = document.getElementById('custYear');
    function curMode(){ for (var i=0;i<modeRadios.length;i++){ if(modeRadios[i].checked) return modeRadios[i].value; } return 'cargo'; }
    Array.prototype.forEach.call(modeRadios, function(r){ r.addEventListener('change', function(){ yearInput.disabled = (curMode()==='cargo'); }); });
    document.getElementById('custQueryBtn').addEventListener('click', function(){
      var btn=this, number=document.getElementById('custNumber').value.trim();
      var result=document.getElementById('custQueryResult');
      if(!number){ showToast('번호를 입력해주세요.', true); return; }
      var orig=btn.textContent; btn.disabled=true; btn.textContent='조회 중…';
      result.innerHTML='<div class="card-sub">관세청에 조회 중…</div>';
      postJSON('/api/customs/query', {mode:curMode(), number:number, year:yearInput.value.trim()}).then(function(d){
        result.innerHTML = renderCustQuery(d);
      }).catch(function(e){
        result.innerHTML = '<div class="cust-err">조회 실패: '+escapeXML(e.message||'')+'</div>';
      }).then(function(){ btn.disabled=false; btn.textContent=orig; });
    });
  })();

  function renderCustQuery(d){
    var html='';
    if (d.api001_error){ html += '<div class="cust-err">[API001] '+escapeXML(d.api001_error)+'</div>'; }
    if (d.api001){
      var s=d.api001;
      if (s.summary && s.summary.length){
        html += '<div class="cust-chips">';
        s.summary.forEach(function(it){ html += '<div class="cust-chip"><span>'+escapeXML(it.label)+'</span> <b>'+escapeXML(it.value)+'</b></div>'; });
        html += '</div>';
      }
      if (s.timeline && s.timeline.length){
        html += '<h3 style="font-size:13px;margin:12px 0 4px;">처리 타임라인 ('+s.timeline.length+'건)</h3><ul class="cust-timeline">';
        s.timeline.forEach(function(t){
          html += '<li><span class="tl-when">'+escapeXML(t.when||'')+'</span> '+escapeXML(t.stage||'')+(t.extra?' <span style="color:var(--text-muted)">| '+escapeXML(t.extra)+'</span>':'')+'</li>';
        });
        html += '</ul>';
      }
    }
    if (d.containers && d.containers.length){
      html += '<h3 style="font-size:13px;margin:12px 0 4px;">컨테이너 상세 (화물번호 '+escapeXML(d.cargo_no||'')+')</h3><div class="cust-chips">';
      d.containers.forEach(function(c){ html += '<div class="cust-chip"><b>'+escapeXML(c.no)+'</b> <span>규격 '+escapeXML(c.size)+' · 봉인 '+escapeXML(c.seal)+'</span></div>'; });
      html += '</div>';
    } else if (d.containers_error){
      html += '<div class="card-sub" style="margin-top:8px;">'+escapeXML(d.containers_error)+'</div>';
    }
    return html || '<div class="card-sub">결과가 없어요.</div>';
  }

  // ═══════════════ 통관: 트래킹 보드 ═══════════════
  function renderBoard(){
    var wrap = document.getElementById('boardCols');
    var byStage = {'입항예정':[],'통관진행중':[],'통관완료':[]};
    boardRows.forEach(function(r){ var st=r['상태']||'입항예정'; if(!byStage[st]) st='입항예정'; byStage[st].push(r); });

    // 입항예정건은 예상입항일이 빠른 순. 날짜가 없는 건(미정)은 맨 뒤로.
    function etaKey(r){
      var d = (r['예상입항일']||'').trim().replace(/[./]/g,'-');
      return /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(d) ? d : '9999-99-99';
    }
    byStage['입항예정'].sort(function(a,b){
      var ka=etaKey(a), kb=etaKey(b);
      if (ka!==kb) return ka<kb ? -1 : 1;
      return (a['주문명']||'').localeCompare(b['주문명']||'');
    });
    wrap.innerHTML = CUST_STAGES.map(function(stage){
      var label = stage==='입항예정' ? '입항예정건' : stage;
      var cards = byStage[stage];
      var body = cards.length ? cards.map(cardHTML).join('') : '<div class="board-empty">해당 없음</div>';
      return '<div class="board-col">'+
        '<div class="board-col-head" style="background:'+CUST_STAGE_COLORS[stage]+'">'+label+' ('+cards.length+')</div>'+
        '<div class="board-col-body">'+body+'</div></div>';
    }).join('');
    Array.prototype.forEach.call(wrap.querySelectorAll('.bc-detail'), function(b){ b.addEventListener('click', function(){ showCardDetail(boardRows[+b.getAttribute('data-i')]); }); });
    Array.prototype.forEach.call(wrap.querySelectorAll('.bc-edit'), function(b){ b.addEventListener('click', function(){ showCardEdit(boardRows[+b.getAttribute('data-i')]); }); });
    Array.prototype.forEach.call(wrap.querySelectorAll('.bc-logi'), function(b){ b.addEventListener('click', function(){ showLogistics(boardRows[+b.getAttribute('data-i')]); }); });
  }
  // 통관진행중 / 통관완료 건에만 물류입고정보 버튼을 붙인다
  function hasLogistics(r){
    var s = r['상태'];
    return s === '통관진행중' || s === '통관완료';
  }

  function cardHTML(r){
    var i = boardRows.indexOf(r);
    var color = CUST_STAGE_COLORS[r['상태']]||'#64748b';
    var h = '<div class="board-card">';
    h += '<div class="bc-top">'+escapeXML((r['제조사']||'')+' · '+(r['주문명']||'(주문명 없음)'))+'</div>';
    if (r['BL번호']){
      h += '<div class="bc-bl">BL '+escapeXML(r['BL번호'])+'</div>';
    } else {
      h += '<div class="bc-nobl">📄 BL 미발행 <span>선하증권 대기</span></div>';
    }
    if (r['화물관리번호']) h += '<div class="bc-bl">화물번호 '+escapeXML(r['화물관리번호'])+'</div>';
    if (r['통관진행상태']) h += '<div class="bc-status" style="color:'+color+'">● '+escapeXML(r['통관진행상태'])+'</div>';
    if (r['수입신고']) h += '<div class="bc-decl">수입신고: '+escapeXML(r['수입신고'])+'</div>';
    if (r['이상사항']) h += '<div class="bc-anomaly">⚠️ 확인필요: '+escapeXML(r['이상사항'])+'</div>';
    if (r['입항일']) h += '<div class="bc-bl">입항일 '+escapeXML(r['입항일'])+'</div>';
    // 아직 입항 전이면 예상입항일과 남은 일수를 보여줌 (정렬 기준이 눈에 보이게)
    if (r['상태']==='입항예정'){
      var eta = (r['예상입항일']||'').trim();
      if (eta){
        var d = dnum(eta.replace(/[./]/g,'-'));
        var extra = '';
        if (d){
          var n = dayDiff(d, todayD());
          extra = ' <b class="'+(n<0?'eta-late':(n<=14?'eta-soon':'eta-ok'))+'">'+
                  (n<0 ? 'D+'+(-n) : (n===0 ? 'D-DAY' : 'D-'+n))+'</b>';
        }
        h += '<div class="bc-eta">예상입항 '+escapeXML(eta)+extra+'</div>';
      } else {
        h += '<div class="bc-eta">예상입항 <b class="eta-none">미정</b></div>';
      }
    }
    if (hasLogistics(r)){
      h += '<button class="bc-logi" data-i="'+i+'">📦 물류입고정보</button>';
    }
    h += '<div class="bc-foot"><span class="bc-time">'+escapeXML(r['갱신시각']||'미조회')+'</span>';
    h += '<span class="bc-btns"><button class="bc-mini bc-edit" data-i="'+i+'">수정</button><button class="bc-mini bc-detail" data-i="'+i+'">상세</button></span></div>';
    return h + '</div>';
  }

  function openModal(html){
    var root=document.getElementById('modalRoot');
    root.innerHTML = '<div class="modal-back">'+html+'</div>';
    var back = root.querySelector('.modal-back');
    back.addEventListener('click', function(e){ if(e.target===back) closeModal(); });
    return root.querySelector('.modal');
  }
  function closeModal(){ document.getElementById('modalRoot').innerHTML=''; }

  function showCardDetail(r){
    var lines=[];
    lines.push('■ '+(r['제조사']||'')+' · '+(r['주문명']||''));
    lines.push('BL번호: '+(r['BL번호']||'')+'   화물관리번호: '+(r['화물관리번호']||'-'));
    lines.push('');
    lines.push('단계(칸): '+(r['상태']||''));
    lines.push('통관진행상태: '+(r['통관진행상태']||'-'));
    lines.push('수입신고: '+(r['수입신고']||'-'));
    lines.push('⚠ 확인필요: '+(r['이상사항']||'없음'));
    lines.push('입항일: '+(r['입항일']||'-')+'   반출일: '+(r['반출일']||'-'));
    lines.push('');
    lines.push('=== 처리 타임라인 ===');
    var prog=r['통관진행사항']||'';
    if(prog){ prog.split(' / ').forEach(function(seg){ lines.push(' · '+seg); }); }
    else lines.push(' (아직 새로고침 안 했거나 이력이 없어요)');
    lines.push('');
    lines.push('갱신시각: '+(r['갱신시각']||'미조회'));
    var m = openModal('<div class="modal"><h3>통관 상세 - '+escapeXML(r['BL번호']||'')+'</h3><div class="modal-pre">'+escapeXML(lines.join('\n'))+'</div><div class="modal-actions"><button class="cust-btn ghost" id="mClose">닫기</button></div></div>');
    m.querySelector('#mClose').addEventListener('click', closeModal);
  }

  function showCardEdit(r){
    if(!r._sheet_row){ showToast('시트 행 위치를 몰라요. ‘① 시트에서 주문 불러오기’를 먼저 눌러주세요.', true); return; }
    var rowsHtml = CUST_HEADER.map(function(h){
      if(h==='상태'){
        var opts=CUST_STAGES.map(function(s){return '<option'+(r[h]===s?' selected':'')+'>'+s+'</option>';}).join('');
        return '<div class="modal-row"><label>'+h+'</label><select data-h="'+h+'">'+opts+'</select></div>';
      }
      return '<div class="modal-row"><label>'+h+'</label><input data-h="'+h+'" value="'+escapeXML(r[h]||'')+'"></div>';
    }).join('');
    var m=openModal('<div class="modal"><h3>행 수정 - '+escapeXML(r['BL번호']||'')+' (시트 '+r._sheet_row+'행)</h3>'+rowsHtml+
      '<div class="modal-actions"><button class="edit-del-btn" id="mDel">이 행 삭제</button><button class="cust-btn ghost" id="mCancel">취소</button><button class="cust-btn" id="mSave">저장</button></div></div>');
    m.querySelector('#mCancel').addEventListener('click', closeModal);
    m.querySelector('#mSave').addEventListener('click', function(){
      var values={}; Array.prototype.forEach.call(m.querySelectorAll('[data-h]'), function(el){ values[el.getAttribute('data-h')]=el.value.trim(); });
      if(!values['BL번호']){ showToast('BL번호는 비울 수 없어요.', true); return; }
      var self=this; self.disabled=true; self.textContent='저장 중…';
      postJSON('/api/customs/edit', {sheet_row:r._sheet_row, values:values}).then(function(d){ boardRows=d.rows; renderBoard(); closeModal(); showToast('저장 완료 (시트 반영됨)'); }).catch(function(e){ showToast(e.message||'실패', true); self.disabled=false; self.textContent='저장'; });
    });
    m.querySelector('#mDel').addEventListener('click', function(){
      if(!confirm('이 행을 구글시트에서 완전히 삭제할까요? 되돌릴 수 없어요.')) return;
      postJSON('/api/customs/delete', {sheet_row:r._sheet_row}).then(function(d){ boardRows=d.rows; renderBoard(); closeModal(); showToast('삭제 완료'); }).catch(function(e){ showToast(e.message||'실패', true); });
    });
  }

  // ═══════════════ 물류입고정보 ═══════════════
  var LOGI_COLS = ['품목명','카톤입수','카톤수량','수량(유닛)','파렛트','파렛트별박스수','소비기한'];

  function todayLabel(){
    var d = new Date(), w = ['일','월','화','수','목','금','토'][d.getDay()];
    var mm = d.getMonth()+1, dd = d.getDate();
    return d.getFullYear()+'-'+(mm<10?'0':'')+mm+'-'+(dd<10?'0':'')+dd+'('+w+')';
  }

  function showLogistics(r){
    var m = openModal('<div class="modal logi-modal"><h3>📦 물류입고정보 — '+escapeXML(r['주문명']||r['BL번호'])+'</h3>'+
      '<div class="card-sub">구글시트에서 불러오는 중…</div></div>');

    postJSON('/api/customs/order', {bl: r['BL번호']}).then(function(d){
      if (!d.found || !d.items.length){
        m.innerHTML = '<h3>📦 물류입고정보</h3>'+
          '<div class="cust-err">이 BL('+escapeXML(r['BL번호']||'')+')에 해당하는 품목을 제조사 탭에서 찾지 못했어요.</div>'+
          '<div class="modal-actions"><button class="cust-btn ghost" id="lgClose">닫기</button></div>';
        m.querySelector('#lgClose').addEventListener('click', closeModal);
        return;
      }
      renderLogistics(m, d, r);
    }).catch(function(e){
      m.innerHTML = '<h3>📦 물류입고정보</h3><div class="cust-err">'+escapeXML(e.message||'')+'</div>'+
        '<div class="modal-actions"><button class="cust-btn ghost" id="lgClose">닫기</button></div>';
      m.querySelector('#lgClose').addEventListener('click', closeModal);
    });
  }

  function renderLogistics(m, d, row){
    var dest = localStorage.getItem('ub_logi_dest') || '등원리';

    function rowsHTML(){
      return d.items.map(function(it,i){
        var pal = (it.cartons && it.boxes_per_pallet) ? (it.cartons/it.boxes_per_pallet) : null;
        return '<tr>'+
          '<td class="lg-name"><input class="lg-nm" data-i="'+i+'" value="'+escapeXML(it.name)+'"></td>'+
          '<td>'+(it.per_carton!=null?fmt(it.per_carton):'—')+'</td>'+
          '<td>'+(it.cartons!=null?fmt(it.cartons):'—')+'</td>'+
          '<td class="lg-qty">'+(it.total!=null?fmt(it.total):'—')+'</td>'+
          '<td class="lg-pal" data-i="'+i+'">'+(pal!=null?(Math.round(pal*100)/100):'—')+'</td>'+
          '<td><input class="lg-bpp" data-i="'+i+'" type="number" min="1" step="1" value="'+
              (it.boxes_per_pallet!=null?it.boxes_per_pallet:'')+'" placeholder="입력"></td>'+
          '<td>'+escapeXML(it.expiry||'—')+'</td></tr>';
      }).join('');
    }

    m.innerHTML =
      '<h3>📦 물류입고정보 — '+escapeXML(d.order_name||d.bl)+'</h3>'+
      '<div class="logi-head">'+
        '<span>작성일자 <b>'+todayLabel()+'</b></span>'+
        '<span>매입처 <b>'+escapeXML(d.supplier)+'</b></span>'+
        '<span>착지 <input id="lgDest" value="'+escapeXML(dest)+'"></span>'+
      '</div>'+
      '<div class="card-sub" style="margin:2px 0 8px;">품목명과 파렛트별 박스수를 고칠 수 있어요. '+
        '파렛트는 자동 계산되고, <b>시트에 저장</b>을 누르면 구글시트에도 그대로 반영됩니다.</div>'+
      '<div style="overflow-x:auto"><table class="logi-table"><thead><tr>'+
        LOGI_COLS.map(function(c){ return '<th>'+c+'</th>'; }).join('')+
      '</tr></thead><tbody>'+rowsHTML()+'</tbody></table></div>'+
      '<div class="modal-actions">'+
        '<button class="cust-btn ghost" id="lgClose">닫기</button>'+
        '<button class="cust-btn ghost" id="lgSave">시트에 저장</button>'+
        '<button class="cust-btn" id="lgPng">PNG로 저장</button>'+
      '</div>';

    // 파렛트별 박스수 입력 -> 파렛트 즉시 재계산
    Array.prototype.forEach.call(m.querySelectorAll('.lg-bpp'), function(inp){
      inp.addEventListener('input', function(){
        var i = +inp.getAttribute('data-i');
        var v = parseFloat(inp.value);
        d.items[i].boxes_per_pallet = (isFinite(v) && v>0) ? v : null;
        var cell = m.querySelector('.lg-pal[data-i="'+i+'"]');
        var c = d.items[i].cartons;
        cell.textContent = (c && d.items[i].boxes_per_pallet)
          ? (Math.round(c/d.items[i].boxes_per_pallet*100)/100) : '—';
      });
    });

    // 품목명 수정 -> 표/이미지에 바로 반영
    Array.prototype.forEach.call(m.querySelectorAll('.lg-nm'), function(inp){
      inp.addEventListener('input', function(){
        d.items[+inp.getAttribute('data-i')].name = inp.value;
      });
    });

    m.querySelector('#lgClose').addEventListener('click', closeModal);
    m.querySelector('#lgDest').addEventListener('change', function(){
      localStorage.setItem('ub_logi_dest', this.value.trim());
    });

    m.querySelector('#lgSave').addEventListener('click', function(){
      var btn=this, orig=btn.textContent; btn.disabled=true; btn.textContent='저장 중…';
      postJSON('/api/customs/order/save', {
        sheet: d.sheet,
        name_col: d.name_col_index,
        pallet_col: d.pallet_col_index,
        items: d.items.map(function(it){
          return {row: it.row, name: it.name,
                  boxes_per_pallet: it.boxes_per_pallet==null ? '' : it.boxes_per_pallet};
        })
      }).then(function(){
        showToast('품목명·파렛트별 박스수를 구글시트에 저장했어요');
      }).catch(function(e){
        showToast(e.message||'저장 실패', true);
      }).then(function(){ btn.disabled=false; btn.textContent=orig; });
    });

    m.querySelector('#lgPng').addEventListener('click', function(){
      var destVal = (m.querySelector('#lgDest').value||'').trim();
      downloadLogisticsPNG(d, destVal);
    });
  }

  // 표를 SVG로 그린 뒤 캔버스로 래스터라이즈해서 PNG 다운로드 (외부 라이브러리 없이)
  function buildLogisticsSVG(d, dest){
    var COLW = [330, 78, 82, 92, 74, 104, 104];
    var W = COLW.reduce(function(a,c){return a+c;},0) + 2;
    var RH = 34, HEAD = 96;
    var H = HEAD + RH*(d.items.length+1) + 14;
    function x(i){ var s=1; for(var k=0;k<i;k++) s+=COLW[k]; return s; }
    function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

    var s = '<svg xmlns="http://www.w3.org/2000/svg" width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'">';
    s += '<rect width="'+W+'" height="'+H+'" fill="#ffffff"/>';
    s += '<style>text{font-family:"Malgun Gothic","맑은 고딕",sans-serif;} '+
         '.h{font-size:14px;fill:#111;} .hb{font-size:14px;font-weight:700;fill:#111;} '+
         '.th{font-size:14px;font-weight:700;fill:#111;} .td{font-size:14px;fill:#111;} '+
         '.qty{font-size:14px;font-weight:700;fill:#1155cc;}</style>';

    // 우측 상단 메타
    var meta = [['작성일자: ', todayLabel()], ['매입처: ', d.supplier], ['착지: ', dest||'']];
    meta.forEach(function(p,i){
      s += '<text class="hb" x="'+(W-10)+'" y="'+(24+i*22)+'" text-anchor="end">'+esc(p[0]+p[1])+'</text>';
    });

    // 헤더행
    var ty = HEAD;
    s += '<rect x="1" y="'+ty+'" width="'+(W-2)+'" height="'+RH+'" fill="#e8e8e8" stroke="#999"/>';
    LOGI_COLS.forEach(function(c,i){
      s += '<text class="th" x="'+(x(i)+COLW[i]/2)+'" y="'+(ty+22)+'" text-anchor="middle">'+esc(c)+'</text>';
      if (i) s += '<line x1="'+x(i)+'" y1="'+ty+'" x2="'+x(i)+'" y2="'+(ty+RH*(d.items.length+1))+'" stroke="#999"/>';
    });

    // 데이터행
    d.items.forEach(function(it, r){
      var y = ty + RH*(r+1);
      var pal = (it.cartons && it.boxes_per_pallet) ? Math.round(it.cartons/it.boxes_per_pallet*100)/100 : '';
      s += '<rect x="1" y="'+y+'" width="'+(W-2)+'" height="'+RH+'" fill="#ffffff" stroke="#999"/>';
      var cells = [it.name, it.per_carton, it.cartons, it.total, pal, it.boxes_per_pallet, it.expiry];
      cells.forEach(function(v,i){
        var txt = (v==null||v==='') ? '' : (typeof v==='number' ? v.toLocaleString('ko-KR') : String(v));
        if (i===0){
          if (txt.length > 24) txt = txt.slice(0,23)+'…';
          s += '<text class="td" x="'+(x(0)+10)+'" y="'+(y+22)+'">'+esc(txt)+'</text>';
        } else {
          s += '<text class="'+(i===3?'qty':'td')+'" x="'+(x(i)+COLW[i]/2)+'" y="'+(y+22)+'" text-anchor="middle">'+esc(txt)+'</text>';
        }
      });
    });
    s += '<rect x="1" y="'+ty+'" width="'+(W-2)+'" height="'+(RH*(d.items.length+1))+'" fill="none" stroke="#999"/>';
    s += '</svg>';
    return {svg:s, w:W, h:H};
  }

  function downloadLogisticsPNG(d, dest){
    var out = buildLogisticsSVG(d, dest);
    var scale = 2;                                   // 선명하게
    var img = new Image();
    var blob = new Blob([out.svg], {type:'image/svg+xml;charset=utf-8'});
    var url = URL.createObjectURL(blob);
    img.onload = function(){
      var cv = document.createElement('canvas');
      cv.width = out.w*scale; cv.height = out.h*scale;
      var ctx = cv.getContext('2d');
      ctx.fillStyle = '#fff'; ctx.fillRect(0,0,cv.width,cv.height);
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);
      cv.toBlob(function(b){
        var a = document.createElement('a');
        a.href = URL.createObjectURL(b);
        a.download = '물류입고정보_'+(d.order_name||d.bl).replace(/[\/:*?"<>|]/g,'_')+'.png';
        document.body.appendChild(a); a.click(); a.remove();
        showToast('PNG로 저장했어요');
      });
    };
    img.onerror = function(){ URL.revokeObjectURL(url); showToast('이미지 생성에 실패했어요', true); };
    img.src = url;
  }

  function showManualAdd(){
    var m=openModal('<div class="modal"><h3>트래킹 보드에 수동 추가</h3>'+
      '<div class="modal-row"><label>BL번호(필수)</label><input id="maBl"></div>'+
      '<div class="modal-row"><label>제조사</label><input id="maMfr"></div>'+
      '<div class="modal-row"><label>주문명</label><input id="maOrder"></div>'+
      '<div class="modal-row"><label>선적일</label><input id="maShipped" placeholder="YYYY-MM-DD"></div>'+
      '<div class="modal-actions"><button class="cust-btn ghost" id="maCancel">취소</button><button class="cust-btn" id="maAdd">추가</button></div></div>');
    m.querySelector('#maCancel').addEventListener('click', closeModal);
    m.querySelector('#maAdd').addEventListener('click', function(){
      var bl=m.querySelector('#maBl').value.trim();
      if(!bl){ showToast('BL번호는 필수예요.', true); return; }
      var self=this; self.disabled=true; self.textContent='추가 중…';
      postJSON('/api/customs/add', {bl:bl, mfr:m.querySelector('#maMfr').value.trim(), order:m.querySelector('#maOrder').value.trim(), shipped:m.querySelector('#maShipped').value.trim()})
        .then(function(d){ boardRows=d.rows; renderBoard(); closeModal(); showToast('수동 추가 완료'); })
        .catch(function(e){ showToast(e.message||'실패', true); self.disabled=false; self.textContent='추가'; });
    });
  }

  (function(){
    // 통관 보드도 탭을 감추면 없다
    if (!document.getElementById('boardSyncBtn')) return;
    function run(path, btn, okMsg){
      var orig=btn.textContent; btn.disabled=true; btn.textContent='처리 중…';
      setBoardStatus('처리 중…');
      postJSON(path, {}).then(function(d){
        boardRows = d.rows || boardRows; renderBoard(); cacheBoard();
        setBoardStatus(okMsg(d));
      }).catch(function(e){
        setBoardStatus('오류: '+(e.message||''));
        showToast(e.message||'실패', true);
      }).then(function(){ btn.disabled=false; btn.textContent=orig; });
    }
    document.getElementById('boardSyncBtn').addEventListener('click', function(){
      run('/api/customs/sync', this, function(d){ return '불러오기 완료: 주문 '+d.total+'건 중 신규 '+d.new_n+'건 추가. 이제 ‘② 관세청 상태 새로고침’을 누르세요.'; });
    });
    document.getElementById('boardRefreshBtn').addEventListener('click', function(){
      run('/api/customs/refresh', this, function(d){ return '새로고침 완료: '+(d.rows?d.rows.length:0)+'건 조회, 단계변경 '+d.changed+'건. 결과는 구글시트에도 저장됐어요.'; });
    });
    document.getElementById('boardAddBtn').addEventListener('click', showManualAdd);
    document.getElementById('boardSheetBtn').addEventListener('click', function(){
      var url = (customsStatus && customsStatus.spreadsheet_url) || 'https://docs.google.com';
      window.open(url, '_blank');
    });
  })();

  // ═══════════════ 트래킹 보드 자동 새로고침 (2시간) ═══════════════
  var AUTO_MS = 2*60*60*1000;
  var LS_ROWS = 'ub_board_rows', LS_AT = 'ub_board_at';

  function cacheBoard(){
    try{
      localStorage.setItem(LS_ROWS, JSON.stringify(boardRows));
      localStorage.setItem(LS_AT, String(Date.now()));
    }catch(e){}
  }
  function restoreBoard(){
    try{
      var raw = localStorage.getItem(LS_ROWS);
      if (!raw) return false;
      var rows = JSON.parse(raw);
      if (!rows || !rows.length) return false;
      boardRows = rows; renderBoard();
      var at = parseInt(localStorage.getItem(LS_AT)||'0',10);
      setBoardStatus('저장된 내역을 표시 중이에요'+(at?' (마지막 갱신 '+new Date(at).toLocaleString('ko-KR')+')':'')+' · 최신 상태를 불러오는 중…');
      return true;
    }catch(e){ return false; }
  }

  // 시트 불러오기 -> 관세청 새로고침을 이어서 실행
  var autoBusy = false;
  function autoRefreshBoard(reason){
    if (autoBusy) return Promise.resolve();
    autoBusy = true;
    setBoardStatus((reason||'')+' 시트에서 주문을 불러오는 중…');
    return postJSON('/api/customs/sync', {})
      .then(function(d){
        boardRows = d.rows || boardRows; renderBoard(); cacheBoard();
        setBoardStatus('주문 '+d.total+'건 (신규 '+d.new_n+'건) · 관세청 상태를 조회하는 중…');
        return postJSON('/api/customs/refresh', {});
      })
      .then(function(d){
        boardRows = d.rows || boardRows; renderBoard(); cacheBoard();
        setBoardStatus('자동 갱신 완료 · '+new Date().toLocaleString('ko-KR')+
                       ' · '+(d.rows?d.rows.length:0)+'건, 단계변경 '+d.changed+'건 (2시간마다 자동 갱신)');
      })
      .catch(function(e){
        var at = parseInt(localStorage.getItem(LS_AT)||'0',10);
        setBoardStatus('자동 갱신 실패: '+(e.message||'')+
                       (at?' · 마지막 성공 '+new Date(at).toLocaleString('ko-KR')+' 내역을 표시 중':''));
      })
      .then(function(){ autoBusy = false; });
  }

  (function initBoardAuto(){
    if (!document.getElementById('boardSyncBtn')) return;   // 통관 탭을 감췄으면 건너뛴다
    restoreBoard();                     // 캐시가 있으면 즉시 표시(항상 내역이 보이게)
    autoRefreshBoard('시작:');            // 그 다음 최신화
    setInterval(function(){ autoRefreshBoard('자동:'); }, AUTO_MS);
    // 절전/탭 복귀 후 2시간이 지났으면 바로 갱신
    document.addEventListener('visibilitychange', function(){
      if (document.hidden) return;
      var at = parseInt(localStorage.getItem(LS_AT)||'0',10);
      if (!at || Date.now()-at > AUTO_MS) autoRefreshBoard('복귀:');
    });
  })();

  applyData(DATA);
})();
