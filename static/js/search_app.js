function copyToClipboard(text, btnElement) {
    navigator.clipboard.writeText(text).then(() => {
        const originalHTML = btnElement.innerHTML;
        btnElement.innerHTML = `
            <svg class="w-3.5 h-3.5 text-emerald-500 scale-110 transition-transform" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12"></polyline>
            </svg>
        `;
        btnElement.classList.remove('bg-warm-50', 'border-warm-200', 'text-warm-500');
        btnElement.classList.add('bg-emerald-50', 'border-emerald-200');
        
        setTimeout(() => {
            btnElement.innerHTML = originalHTML;
            btnElement.classList.remove('bg-emerald-50', 'border-emerald-200');
            btnElement.classList.add('bg-warm-50', 'border-warm-200', 'text-warm-500');
        }, 2000);
    }).catch(err => {
        console.error('Failed to copy text: ', err);
    });
}

// Search input visual clear handler
const searchInput = document.getElementById('search-input');
const clearSearchBtn = document.getElementById('clear-search');

if (searchInput && clearSearchBtn) {
    searchInput.addEventListener('input', () => {
        clearSearchBtn.classList.toggle('hidden', !searchInput.value);
    });

    clearSearchBtn.addEventListener('click', () => {
        searchInput.value = '';
        clearSearchBtn.classList.add('hidden');
        searchInput.focus();
    });
}

// Set date limits
(function() {
    const today = new Date().toISOString().split('T')[0];
    document.querySelectorAll('input[type="date"]').forEach(input => {
        input.max = today;
    });
})();

function toggleSearchHelp() {
    const help = document.getElementById('search-help');
    if (help) help.classList.toggle('hidden');
}

function toggleTimeline() {
    const timeline = document.getElementById('timeline-container');
    const dateFrom = document.getElementById('f-date-from').value;
    const dateTo = document.getElementById('f-date-to').value;

    if (timeline) {
        if (dateFrom || dateTo) {
            timeline.classList.remove('hidden');
        } else {
            timeline.classList.toggle('hidden');
        }
    }
}

function toggleDropdown(id) {
    document.querySelectorAll('.dropdown-container div[id]').forEach(d => {
        if (d.id !== id) d.classList.add('hidden');
    });
    const drop = document.getElementById(id);
    if (drop) drop.classList.toggle('hidden');
}

function setFilter(name, value) {
    const input = document.getElementById(`f-${name.replace('_', '-')}`);
    if (input) {
        input.value = value;
        document.getElementById('search-form').submit();
    }
}

function syncAndSubmit(input, hiddenId, displayId) {
    if (input.value) {
        document.getElementById(hiddenId).value = input.value;
        document.getElementById(displayId).innerText = input.value.split('-').join('.');
        document.getElementById('search-form').submit();
    }
}

function openDatePicker(container, defaultDate, displayId) {
    const input = container.querySelector('input[type="date"]');
    if (input) {
        if (!input.value) {
            input.value = defaultDate;
        }
        input.showPicker();
    }
}

function clearDates(form) {
    document.getElementById('f-date-from').value = '';
    document.getElementById('f-date-to').value = '';
    form.submit();
}

// Close dropdowns on click outside
window.addEventListener('click', function(e) {
    if (e.target && !e.target.closest('.dropdown-container')) {
        document.querySelectorAll('.dropdown-container div[id]').forEach(d => {
            d.classList.add('hidden');
        });
    }
});

// Timeline Logic
(function() {
    const sliderFrom = document.getElementById('timeline-slider-from');
    const sliderTo = document.getElementById('timeline-slider-to');
    const rangeBar = document.getElementById('timeline-range-bar');
    const dateFromInput = document.getElementById('f-date-from');
    const dateToInput = document.getElementById('f-date-to');
    const rangeText = document.getElementById('timeline-range-text');
    
    if (!sliderFrom || !sliderTo) return;

    const startTimestamp = new Date('2020-01-01T00:00:00').getTime();
    const now = new Date();
    now.setHours(23, 59, 59, 999);
    const endTimestamp = now.getTime();
    const totalDuration = endTimestamp - startTimestamp;

    function updateTimelineVisuals() {
        let valFrom = parseInt(sliderFrom.value);
        let valTo = parseInt(sliderTo.value);
        if (valFrom > valTo) { [valFrom, valTo] = [valTo, valFrom]; }

        rangeBar.style.left = (valFrom / 10) + '%';
        rangeBar.style.right = (100 - (valTo / 10)) + '%';
        
        const d1 = new Date(startTimestamp + (valFrom / 1000) * totalDuration);
        const d2 = new Date(startTimestamp + (valTo / 1000) * totalDuration);
        const d1s = d1.toISOString().split('T')[0];
        const d2s = d2.toISOString().split('T')[0];
        rangeText.innerText = d1s.split('-').join('.') + ' — ' + d2s.split('-').join('.');
        return {d1s, d2s};
    }

    function setSlidersFromInputs() {
        const v1 = dateFromInput.value || '2020-01-01';
        const v2 = dateToInput.value || new Date().toISOString().split('T')[0];
        sliderFrom.value = (new Date(v1).getTime() - startTimestamp) / totalDuration * 1000;
        sliderTo.value = (new Date(v2).getTime() - startTimestamp) / totalDuration * 1000;
        updateTimelineVisuals();
    }

    sliderFrom.oninput = updateTimelineVisuals;
    sliderTo.oninput = updateTimelineVisuals;
    
    sliderFrom.onchange = () => {
        const {d1s, d2s} = updateTimelineVisuals();
        dateFromInput.value = d1s;
        dateToInput.value = d2s;
        document.getElementById('search-form').submit();
    };
    
    sliderTo.onchange = () => {
        const {d1s, d2s} = updateTimelineVisuals();
        dateFromInput.value = d1s;
        dateToInput.value = d2s;
        document.getElementById('search-form').submit();
    };

    setSlidersFromInputs();
})();

let activeChunkId = null;

function playFragment(videoId, startSec, title, ts, chunkId) {
    // Highlight active card
    if (activeChunkId) {
        const oldCard = document.getElementById(`chunk-${activeChunkId}`);
        if (oldCard) oldCard.classList.remove('ring-2', 'ring-brand-400', 'bg-[#FDFDFB]');
    }
    activeChunkId = chunkId;
    const newCard = document.getElementById(`chunk-${chunkId}`);
    if (newCard) newCard.classList.add('ring-2', 'ring-brand-400', 'bg-[#FDFDFB]');

    const video = document.getElementById('main-video');
    const playerPlaceholder = document.getElementById('player-placeholder');
    const playerContent = document.getElementById('player-content');
    const playerTitle = document.getElementById('player-title');
    const playerMeta = document.getElementById('player-meta');

    if (playerPlaceholder) playerPlaceholder.classList.add('hidden');
    if (playerContent) playerContent.classList.remove('hidden');

    if (playerTitle) playerTitle.textContent = title;
    if (playerMeta) playerMeta.textContent = ts;

    if (video) {
        video.src = `/videos/${videoId}/file#t=${startSec}`;
        video.play();
    }
    
    // Visual cue on video
    const overlay = document.getElementById('video-overlay');
    if (overlay) {
        overlay.classList.add('opacity-100');
        setTimeout(() => overlay.classList.remove('opacity-100'), 500);
    }

    // Scroll player into view on mobile
    if (window.innerWidth < 1024) {
        const pCol = document.getElementById('player-column');
        if (pCol) pCol.scrollIntoView({ behavior: 'smooth' });
    }
}

function closePlayer() {
    if (activeChunkId) {
        const card = document.getElementById(`chunk-${activeChunkId}`);
        if (card) card.classList.remove('ring-2', 'ring-brand-400', 'bg-[#FDFDFB]');
        activeChunkId = null;
    }

    const video = document.getElementById('main-video');
    const playerPlaceholder = document.getElementById('player-placeholder');
    const playerContent = document.getElementById('player-content');

    if (video) {
        video.pause();
        video.src = "";
    }
    
    if (playerContent) playerContent.classList.add('hidden');
    if (playerPlaceholder) playerPlaceholder.classList.remove('hidden');
}

async function editChunk(chunkId, currentText, btn) {
    const card = btn.closest('.result-card-ui');
    if (!card) return;
    const textP = card.querySelector('p');
    if (!textP) return;
    
    const originalOnClick = card.onclick;
    card.onclick = null;

    const textarea = document.createElement('textarea');
    textarea.className = "w-full bg-[#FAF9F6] border border-brand-300 rounded-xl py-3 px-4 text-sm focus:outline-none focus:bg-white transition-all shadow-inner mb-3 italic";
    textarea.rows = 4;
    textarea.value = currentText;
    
    const actionsDiv = document.createElement('div');
    actionsDiv.className = "flex gap-2 mb-3";
    actionsDiv.innerHTML = `
        <button class="px-4 py-2 bg-brand-500 text-white text-[10px] font-black rounded-lg uppercase tracking-widest hover:bg-brand-600 transition-all shadow-md">Сохранить</button>
        <button class="px-4 py-2 bg-warm-100 text-warm-500 text-[10px] font-black rounded-lg uppercase tracking-widest hover:bg-warm-200 transition-all">Отмена</button>
    `;

    const saveBtn = actionsDiv.children[0];
    const cancelBtn = actionsDiv.children[1];

    textP.style.display = 'none';
    textP.after(actionsDiv);
    textP.after(textarea);
    textarea.focus();

    cancelBtn.onclick = (e) => {
        e.stopPropagation();
        textarea.remove();
        actionsDiv.remove();
        textP.style.display = 'block';
        card.onclick = originalOnClick;
    };

    saveBtn.onclick = async (e) => {
        e.stopPropagation();
        const newText = textarea.value;
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<svg class="animate-spin h-3 w-3 text-white inline mr-1" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>...';
        
        try {
            const res = await fetch(`/api/chunks/${chunkId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: newText })
            });
            
            if (res.ok) {
                textP.textContent = `«${newText}»`;
                textarea.remove();
                actionsDiv.remove();
                textP.style.display = 'block';
                card.onclick = originalOnClick;
                const editBtn = card.querySelector('button[onclick*="editChunk"]');
                if (editBtn) {
                    editBtn.setAttribute('onclick', `event.stopPropagation(); editChunk('${chunkId}', \`${newText.replace(/`/g, '\\`')}\`, this)`);
                }
                
                card.classList.add('ring-2', 'ring-green-400');
                setTimeout(() => card.classList.remove('ring-2', 'ring-green-400'), 1500);
            } else {
                alert('Ошибка при сохранении');
                saveBtn.disabled = false;
                saveBtn.textContent = 'Сохранить';
            }
        } catch (err) {
            alert('Ошибка связи');
            saveBtn.disabled = false;
            saveBtn.textContent = 'Сохранить';
        }
    };
}

const speakerModal = document.getElementById('speaker-modal');
const speakerList = document.getElementById('speaker-list');
const speakerTitle = document.getElementById('speaker-video-title');

async function saveSpeaker(videoId, tag, name, inputEl) {
    try {
        inputEl.classList.add('ring-2', 'ring-brand-200');
        const res = await fetch(`/api/videos/${videoId}/speakers`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tag, name })
        });
        if (res.ok) {
            inputEl.classList.remove('ring-brand-200');
            inputEl.classList.add('ring-2', 'ring-green-400', 'bg-green-50');
            setTimeout(() => {
                inputEl.classList.remove('ring-2', 'ring-green-400', 'bg-green-50');
            }, 1000);
        }
    } catch (e) {
        inputEl.classList.add('ring-2', 'ring-red-400');
        alert('Ошибка при сохранении имени');
    }
}

async function manageSpeakers(videoId, title) {
    if (!speakerModal) return;
    speakerModal.classList.remove('hidden');
    if (speakerTitle) speakerTitle.textContent = title;
    if (speakerList) speakerList.innerHTML = '<div class="text-center py-10 animate-pulse text-warm-400 font-bold uppercase text-[10px] tracking-widest">Анализ аудио-профилей...</div>';
    
    try {
        const response = await fetch(`/api/videos/${videoId}/speakers`);
        const speakers = await response.json();
        
        let html = `
            <div class="mb-6 p-4 bg-brand-50 border border-brand-100 rounded-2xl flex items-start gap-3">
                <div class="w-8 h-8 bg-brand-500 text-white rounded-lg flex items-center justify-center shrink-0 shadow-sm">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"></path></svg>
                </div>
                <div>
                    <h4 class="text-xs font-bold text-brand-700 mb-1 uppercase tracking-tight">Голосовые отпечатки</h4>
                    <p class="text-[10px] text-brand-600 leading-relaxed italic">При сохранении имени система запоминает уникальный тембр голоса. В будущем этот человек будет распознаваться автоматически во всех новых видео.</p>
                </div>
            </div>
            <div class="space-y-4">
        `;
        
        if (speakers.length === 0) {
            html = '<div class="text-center py-10 text-warm-400 italic text-sm">В этом видео не обнаружено разных голосов.</div>';
        } else {
            speakers.forEach(s => {
                html += `
                    <div class="group relative bg-[#FAF9F6] p-5 rounded-[1.5rem] border border-[#E5E1D8] transition-all hover:bg-white hover:shadow-xl hover:-translate-y-0.5">
                        <div class="flex justify-between items-center mb-3">
                            <div class="flex items-center gap-2">
                                <span class="w-6 h-6 bg-warm-200 text-warm-600 rounded-full flex items-center justify-center text-[10px] font-black tracking-tighter">${s.tag}</span>
                                <label class="text-[9px] font-black text-warm-400 uppercase tracking-widest">ID Группы</label>
                            </div>
                            <span class="text-[8px] text-emerald-500 font-bold opacity-0 group-hover:opacity-100 transition-opacity uppercase tracking-widest flex items-center gap-1">
                                <svg class="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"></path></svg>
                                Запомнить
                            </span>
                        </div>
                        <div class="relative">
                            <input type="text" value="${s.name}" 
                                   onchange="saveSpeaker('${videoId}', '${s.tag}', this.value, this)"
                                   class="w-full bg-white border border-[#E5E1D8] rounded-xl py-3.5 px-4 text-sm font-bold text-[#3A3630] focus:outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-500/5 transition-all shadow-sm"
                                   placeholder="Введите ФИО или роль...">
                            <div class="absolute right-4 top-4 text-warm-200">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"></path></svg>
                            </div>
                        </div>
                    </div>
                `;
            });
            html += '</div>';
        }
        if (speakerList) speakerList.innerHTML = html;
    } catch (e) {
        if (speakerList) speakerList.innerHTML = '<div class="text-rose-400 py-10 text-center font-bold">Ошибка связи с сервером.</div>';
    }
}

function closeSpeakers() {
    if (speakerModal) speakerModal.classList.add('hidden');
    // Refresh search to show new names
    const q = new URLSearchParams(window.location.search).get('q');
    if (q) window.location.reload();
}

const SEARCH_MODE_INFO = {
    hybrid: {
        title: "Гибридный поиск",
        text: "Алгоритм объединяет два метода: анализ смысла и поиск по точным словам. Запрос одновременно обрабатывается нейросетью для выявления контекста и базой данных для нахождения буквенных совпадений. Итоговый список формируется путем сложения этих результатов.<br><br><b>Сравнение:</b> В отличие от чисто «Смыслового поиска», этот режим не пропустит редкие имена или термины. В сравнении с поиском «По словам», он находит более широкую выборку, включая фрагменты, подходящие по теме, даже без точных совпадений."
    },
    semantic: {
        title: "Смысловой поиск",
        text: "Поиск основывается на технологии векторных связей. Система переводит ваш запрос в «карту смыслов», находя фрагменты, которые наиболее близки к основной идее вашего вопроса, а не к конкретным буквам. Это позволяет находить ответы на вопросы, даже если в тексте использованы синонимы.<br><br><b>Сравнение:</b> В отличие от поиска «По словам», этот режим понимает идеи, а не просто буквы. В сравнении с «Поиском цитат», он гораздо более гибкий: он найдет фрагмент, даже если вы перепутали порядок слов или пересказали суть своими словами."
    },
    lexical: {
        title: "По словам",
        text: "Метод нахождения прямых словесных совпадений. Система ищет в тексте именно те слова, которые вы ввели. Слова могут встречаться в любой последовательности и на любом расстоянии друг от друга — главное, чтобы они присутствовали в фрагменте.<br><br><b>Сравнение:</b> В отличие от «Смыслового поиска», этот режим строго ограничен вашим запросом и не ищет по синонимам. В сравнении с «Поиском цитат», здесь порядок слов не имеет значения, что позволяет находить упоминания, даже если они разнесены по разным частям предложения."
    },
    quote: {
        title: "Поиск цитат",
        text: "Режим для нахождения конкретных фраз в строгой последовательности. Алгоритм ищет слова именно в том порядке, в котором они указаны в запросе. Допускаются лишь небольшие разрывы (до 60 символов) между словами на знаки препинания или короткие вставки, при этом учитываются изменения окончаний слов.<br><br><b>Сравнение:</b> В отличие от поиска «По словам», этот режим гарантирует сохранение смысла фразы за счет строгого порядка слов. В сравнении с «Гибридным», он игнорирует общий смысл видео, фокусируясь исключительно на буквальном совпадении искомого выражения."
    }
};

function showSearchModeHelp(mode) {
    const info = SEARCH_MODE_INFO[mode];
    if (!info) return;
    const title = document.getElementById('mode-help-title');
    const text = document.getElementById('mode-help-text');
    const modal = document.getElementById('search-mode-modal');
    if (title) title.textContent = info.title;
    if (text) text.innerHTML = info.text;
    if (modal) modal.classList.remove('hidden');
}

function closeSearchModeModal() {
    const modal = document.getElementById('search-mode-modal');
    if (modal) modal.classList.add('hidden');
}
