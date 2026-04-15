<script lang="ts">
	import { toast } from 'svelte-sonner';
	import dayjs from 'dayjs';
	import localizedFormat from 'dayjs/plugin/localizedFormat';
	import { onMount, onDestroy, getContext } from 'svelte';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';

	const i18n = getContext('i18n');
	dayjs.extend(localizedFormat);

	const apiBase = `${window.location.protocol}//${window.location.hostname}:8000`;
	const SCOPE = 'default-scope';

	// ── Memory state ──
	let memories: any[] = [];
	let loading = true;
	let showMemoryModal = false;
	let editingMemoryFilename: string | null = null;
	let memoryName = '';
	let memoryDesc = '';
	let memoryBody = '';
	let showClearConfirmDialog = false;

	const loadMemories = async () => {
		loading = true;
		try {
			const res = await fetch(`${apiBase}/api/v1/memory?scope_id=${encodeURIComponent(SCOPE)}`);
			const data = await res.json();
			memories = data.memories || [];
		} catch (err) {
			toast.error(`${err}`);
			memories = [];
		}
		loading = false;
	};

	function openMemory(mem: any) {
		editingMemoryFilename = mem.filename;
		memoryName = mem.name || '';
		memoryDesc = mem.description || '';
		memoryBody = mem.body || '';
		showMemoryModal = true;
	}

	async function saveMemory() {
		if (!memoryBody.trim()) { toast.error('Заполните текст'); return; }
		const filename = editingMemoryFilename || `mem-${Date.now()}.md`;
		try {
			const res = await fetch(
				`${apiBase}/api/v1/memory/${encodeURIComponent(filename)}?scope_id=${encodeURIComponent(SCOPE)}`,
				{
					method: 'PUT',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ name: memoryName.trim(), description: memoryDesc.trim() || memoryBody.trim().slice(0, 200), body: memoryBody.trim(), type: 'user' })
				}
			);
			if (!res.ok) throw new Error('Save failed');
			showMemoryModal = false;
			toast.success(editingMemoryFilename ? 'Обновлено' : 'Добавлено');
			loadMemories();
		} catch { toast.error('Ошибка сохранения'); }
	}

	async function deleteMemory(filename: string) {
		try {
			const res = await fetch(
				`${apiBase}/api/v1/memory/${encodeURIComponent(filename)}?scope_id=${encodeURIComponent(SCOPE)}`,
				{ method: 'DELETE' }
			);
			if (!res.ok) throw new Error('Delete failed');
			toast.success('Запись удалена');
			loadMemories();
		} catch { toast.error('Ошибка удаления'); }
	}

	const onClearConfirmed = async () => {
		try {
			await Promise.all(memories.map((m: any) =>
				fetch(`${apiBase}/api/v1/memory/${encodeURIComponent(m.filename)}?scope_id=${encodeURIComponent(SCOPE)}`, { method: 'DELETE' })
			));
			toast.success('Память очищена');
			memories = [];
		} catch (error) {
			toast.error(`${error}`);
		}
		showClearConfirmDialog = false;
	};

	// ── SSE ──
	let eventSource: EventSource | null = null;

	function connectSSE() {
		try {
			eventSource = new EventSource(`${apiBase}/api/v1/memory-events?scope_id=${encodeURIComponent(SCOPE)}`);
			eventSource.onmessage = (e) => {
				try {
					const data = JSON.parse(e.data);
					if (data.type === 'connected') return;
					loadMemories();
					if (activeTab === 'instructions') loadInstructions();
				} catch {}
			};
			eventSource.onerror = () => {
				eventSource?.close();
				setTimeout(connectSSE, 10000);
			};
		} catch {}
	}

	// ── Tab state ──
	let activeTab: 'memory' | 'instructions' = 'memory';

	// ── Instructions state ──
	let instructions: any[] = [];
	let instrLoading = false;
	let showInstrModal = false;
	let editingFilename: string | null = null;
	let editingSource: string = 'user';
	let instrDesc = '';
	let instrBody = '';
	let isAutoInstr = false;

	const SOURCE_LABELS: Record<string, string> = { auto: 'Авто', user: 'Ручная' };

	async function loadInstructions() {
		instrLoading = true;
		try {
			const res = await fetch(`${apiBase}/api/v1/instructions?scope_id=${encodeURIComponent(SCOPE)}`);
			const data = await res.json();
			instructions = data.instructions || [];
		} catch (e) {
			console.error('Failed to load instructions', e);
		}
		instrLoading = false;
	}

	function openInstruction(instr: any) {
		editingFilename = instr.filename;
		editingSource = instr.source;
		isAutoInstr = instr.source === 'auto';
		instrDesc = instr.description || '';
		instrBody = instr.body || '';
		showInstrModal = true;
	}

	function openNewInstruction() {
		editingFilename = null;
		editingSource = 'user';
		isAutoInstr = false;
		instrDesc = '';
		instrBody = '';
		showInstrModal = true;
	}

	async function saveInstruction() {
		if (!instrBody.trim()) {
			toast.error('Заполните текст инструкции');
			return;
		}
		const filename = editingFilename || `instr-${Date.now()}.md`;
		try {
			const res = await fetch(
				`${apiBase}/api/v1/instructions/${encodeURIComponent(filename)}?scope_id=${encodeURIComponent(SCOPE)}`,
				{
					method: 'PUT',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						description: instrDesc.trim() || instrBody.trim().slice(0, 200),
						body: instrBody.trim(),
						source: editingSource
					})
				}
			);
			if (!res.ok) throw new Error('Save failed');
			showInstrModal = false;
			toast.success(editingFilename ? 'Инструкция обновлена' : 'Инструкция добавлена');
			loadInstructions();
		} catch {
			toast.error('Ошибка сохранения');
		}
	}

	async function deleteInstruction() {
		if (!editingFilename) return;
		try {
			const res = await fetch(
				`${apiBase}/api/v1/instructions/${encodeURIComponent(editingFilename)}?scope_id=${encodeURIComponent(SCOPE)}`,
				{ method: 'DELETE' }
			);
			if (!res.ok) throw new Error('Delete failed');
			showInstrModal = false;
			toast.success('Инструкция удалена');
			loadInstructions();
		} catch {
			toast.error('Ошибка удаления');
		}
	}

	onMount(() => {
		loadMemories();
		connectSSE();
	});
	onDestroy(() => eventSource?.close());
</script>

<div class="flex flex-col h-full justify-between text-sm">
	<!-- Tab bar -->
	<div class="flex border-b border-white/10 mb-4">
		<button
			class="px-5 py-2.5 text-sm font-medium transition-colors border-b-2 {activeTab === 'memory'
				? 'text-blue-400 border-blue-400'
				: 'text-gray-500 border-transparent hover:text-gray-300'}"
			on:click={() => (activeTab = 'memory')}
		>Память</button>
		<button
			class="px-5 py-2.5 text-sm font-medium transition-colors border-b-2 {activeTab === 'instructions'
				? 'text-blue-400 border-blue-400'
				: 'text-gray-500 border-transparent hover:text-gray-300'}"
			on:click={() => {
				activeTab = 'instructions';
				if (instructions.length === 0 && !instrLoading) loadInstructions();
			}}
		>Инструкции</button>
	</div>

	<!-- Memory panel -->
	{#if activeTab === 'memory'}
		<div class="space-y-3 overflow-y-scroll scrollbar-hidden h-full pr-1.5">
			<div>
				<!-- Memory table -->
				{#if loading}
					<div class="text-center py-8 text-gray-500">Загрузка...</div>
				{:else if memories.length > 0}
					<div class="text-left text-sm w-full overflow-y-auto max-h-[calc(100vh-20rem)]">
						<div class="relative overflow-x-auto">
							<table class="w-full text-sm text-left text-gray-600 dark:text-gray-400 table-auto">
								<thead class="text-xs text-gray-700 uppercase bg-transparent dark:text-gray-200 border-b-2 border-gray-50 dark:border-gray-850/30">
									<tr>
										<th scope="col" class="px-3 py-2">Название</th>
										<th scope="col" class="px-3 py-2 hidden md:table-cell">Изменено</th>
										<th scope="col" class="px-3 py-2 text-right" />
									</tr>
								</thead>
								<tbody>
									{#each memories as memory}
										<tr class="border-b border-gray-50 dark:border-gray-850/30 items-center">
											<td class="px-3 py-1">
												<div class="min-w-0">
													<div class="line-clamp-1 font-medium text-gray-800 dark:text-gray-200">
														{memory.name || memory.description}
													</div>
													{#if memory.description}
														<div class="line-clamp-1 text-xs text-gray-500">{memory.description}</div>
													{/if}
												</div>
											</td>
											<td class="px-3 py-1 hidden md:table-cell h-[2.5rem]">
												<div class="my-auto whitespace-nowrap text-xs">
													{dayjs(memory.mtime * 1000).format('LLL')}
												</div>
											</td>
											<td class="px-3 py-1">
												<div class="flex justify-end w-full">
													<Tooltip content={'Редактировать'}>
														<button
															class="self-center w-fit text-sm px-2 py-2 hover:bg-black/5 dark:hover:bg-white/5 rounded-xl"
															on:click={() => openMemory(memory)}
														>
															<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-4 h-4">
																<path stroke-linecap="round" stroke-linejoin="round" d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L6.832 19.82a4.5 4.5 0 0 1-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 0 1 1.13-1.897L16.863 4.487Zm0 0L19.5 7.125" />
															</svg>
														</button>
													</Tooltip>
													<Tooltip content={'Удалить'}>
														<button
															class="self-center w-fit text-sm px-2 py-2 hover:bg-black/5 dark:hover:bg-white/5 rounded-xl"
															on:click={() => deleteMemory(memory.filename)}
														>
															<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-4 h-4">
																<path stroke-linecap="round" stroke-linejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
															</svg>
														</button>
													</Tooltip>
												</div>
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</div>
				{:else}
					<div class="text-center py-12 text-gray-500">
						Воспоминания, доступные ассистенту, будут отображаться здесь.
					</div>
				{/if}
			</div>
		</div>

		<div class="flex text-sm font-medium gap-1.5 pt-3">
			<button
				class="px-3.5 py-1.5 font-medium hover:bg-black/5 dark:hover:bg-white/5 outline outline-1 outline-gray-100 dark:outline-gray-800 rounded-3xl"
				on:click={() => { editingMemoryFilename = null; memoryName = ''; memoryDesc = ''; memoryBody = ''; showMemoryModal = true; }}
			>Добавить</button>
			<button
				class="px-3.5 py-1.5 font-medium text-red-500 hover:bg-black/5 dark:hover:bg-white/5 outline outline-1 outline-red-100 dark:outline-red-800 rounded-3xl"
				on:click={() => { if (memories.length > 0) { showClearConfirmDialog = true; } else { toast.error('Нет воспоминаний для удаления'); } }}
			>Очистить память</button>
		</div>
	{/if}

	<!-- Instructions panel -->
	{#if activeTab === 'instructions'}
		<div class="overflow-y-scroll scrollbar-hidden h-full pr-1.5">
			<button
				class="inline-flex items-center gap-1.5 px-4 py-2 rounded-md border border-dashed border-white/15 bg-transparent text-gray-400 text-sm cursor-pointer transition-colors hover:border-blue-400 hover:text-blue-400 mb-3"
				on:click={openNewInstruction}
			>+ Добавить инструкцию</button>

			{#if instrLoading}
				<div class="text-center py-8 text-gray-500">Загрузка...</div>
			{:else if instructions.length === 0}
				<div class="text-center py-10 text-gray-500 text-sm">Пока нет инструкций</div>
			{:else}
				{#each instructions as instr}
					<button
						class="flex items-center gap-3 w-full px-4 py-3 border-b border-white/5 cursor-pointer transition-colors hover:bg-white/[.04] text-left"
						on:click={() => openInstruction(instr)}
					>
						<div class="flex-1 min-w-0">
							<div class="text-sm font-medium truncate">{instr.name}</div>
							<div class="text-xs text-gray-500 mt-0.5 line-clamp-1">{instr.description}</div>
						</div>
						<span
							class="text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wider flex-shrink-0 {instr.source === 'auto'
								? 'bg-yellow-500/15 text-yellow-500'
								: 'bg-blue-500/15 text-blue-400'}"
						>{SOURCE_LABELS[instr.source] || instr.source}</span>
					</button>
				{/each}
			{/if}
		</div>
	{/if}
</div>

<!-- Instruction modal -->
{#if showInstrModal}
	<!-- svelte-ignore a11y-click-events-have-key-events -->
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<div
		class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[10000] flex items-center justify-center"
		on:click|self={() => (showInstrModal = false)}
	>
		<div class="bg-[#1e1e1e] border border-[#333] rounded-xl w-[90%] max-w-[560px] max-h-[85vh] overflow-hidden flex flex-col shadow-2xl">
			<div class="flex items-center justify-between px-6 pt-5 pb-4 border-b border-[#333]">
				<h3 class="text-base font-semibold">{editingFilename ? (isAutoInstr ? instrDesc || 'Инструкция' : 'Редактировать') : 'Новая инструкция'}</h3>
				<button
					class="w-7 h-7 rounded-md border border-[#333] bg-transparent text-gray-400 flex items-center justify-center text-base hover:bg-[#333] hover:text-gray-200"
					on:click={() => (showInstrModal = false)}
				>&times;</button>
			</div>
			<div class="px-6 py-5 overflow-y-auto flex-1 space-y-3.5">
				<div>
					<label class="block text-[11px] font-medium text-gray-400 mb-1.5 uppercase tracking-wider">Описание</label>
					<input
						bind:value={instrDesc}
						disabled={isAutoInstr}
						class="w-full px-3 py-2.5 rounded-md border border-[#333] bg-[#1a1a1a] text-gray-200 text-sm outline-none transition-colors focus:border-blue-400 disabled:opacity-50"
						type="text"
						placeholder="Краткое описание правила"
					/>
				</div>
				<div>
					<label class="block text-[11px] font-medium text-gray-400 mb-1.5 uppercase tracking-wider">Правило</label>
					<textarea
						bind:value={instrBody}
						disabled={isAutoInstr}
						class="w-full px-3 py-2.5 rounded-md border border-[#333] bg-[#1a1a1a] text-gray-200 text-sm outline-none transition-colors focus:border-blue-400 disabled:opacity-50 min-h-[120px] resize-y leading-relaxed"
						placeholder="Текст инструкции"
					></textarea>
				</div>
			</div>
			<div class="flex items-center justify-between px-6 py-4 border-t border-[#333]">
				{#if editingFilename}
					<button
						class="px-4 py-2 rounded-md text-sm font-medium text-red-400 border border-red-400 bg-transparent hover:bg-red-400 hover:text-white transition-colors"
						on:click={deleteInstruction}
					>Удалить</button>
				{:else}
					<div></div>
				{/if}
				<div class="flex gap-2">
					<button
						class="px-4 py-2 rounded-md text-sm font-medium text-gray-400 bg-transparent hover:text-gray-200 transition-colors"
						on:click={() => (showInstrModal = false)}
					>Отмена</button>
					{#if !isAutoInstr}
						<button
							class="px-4 py-2 rounded-md text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 transition-colors"
							on:click={saveInstruction}
						>Сохранить</button>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}

<ConfirmDialog
	title={'Очистить память'}
	message={'Вы уверены? Все воспоминания будут удалены. Это действие нельзя отменить.'}
	show={showClearConfirmDialog}
	on:confirm={onClearConfirmed}
	on:cancel={() => { showClearConfirmDialog = false; }}
/>

<!-- Memory edit/create modal -->
{#if showMemoryModal}
	<!-- svelte-ignore a11y-click-events-have-key-events -->
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<div
		class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[10000] flex items-center justify-center"
		on:click|self={() => (showMemoryModal = false)}
	>
		<div class="bg-[#1e1e1e] border border-[#333] rounded-xl w-[90%] max-w-[560px] max-h-[85vh] overflow-hidden flex flex-col shadow-2xl">
			<div class="flex items-center justify-between px-6 pt-5 pb-4 border-b border-[#333]">
				<h3 class="text-base font-semibold">{editingMemoryFilename ? 'Редактировать' : 'Новое воспоминание'}</h3>
				<button
					class="w-7 h-7 rounded-md border border-[#333] bg-transparent text-gray-400 flex items-center justify-center text-base hover:bg-[#333] hover:text-gray-200"
					on:click={() => (showMemoryModal = false)}
				>&times;</button>
			</div>
			<div class="px-6 py-5 overflow-y-auto flex-1 space-y-3.5">
				<div>
					<label class="block text-[11px] font-medium text-gray-400 mb-1.5 uppercase tracking-wider">Название</label>
					<input
						bind:value={memoryName}
						class="w-full px-3 py-2.5 rounded-md border border-[#333] bg-[#1a1a1a] text-gray-200 text-sm outline-none transition-colors focus:border-blue-400"
						type="text"
						placeholder="Краткое название"
					/>
				</div>
				<div>
					<label class="block text-[11px] font-medium text-gray-400 mb-1.5 uppercase tracking-wider">Описание</label>
					<input
						bind:value={memoryDesc}
						class="w-full px-3 py-2.5 rounded-md border border-[#333] bg-[#1a1a1a] text-gray-200 text-sm outline-none transition-colors focus:border-blue-400"
						type="text"
						placeholder="Краткое описание"
					/>
				</div>
				<div>
					<label class="block text-[11px] font-medium text-gray-400 mb-1.5 uppercase tracking-wider">Содержание</label>
					<textarea
						bind:value={memoryBody}
						class="w-full px-3 py-2.5 rounded-md border border-[#333] bg-[#1a1a1a] text-gray-200 text-sm outline-none transition-colors focus:border-blue-400 min-h-[120px] resize-y leading-relaxed"
						placeholder="Текст воспоминания"
					></textarea>
				</div>
			</div>
			<div class="flex items-center justify-between px-6 py-4 border-t border-[#333]">
				{#if editingMemoryFilename}
					<button
						class="px-4 py-2 rounded-md text-sm font-medium text-red-400 border border-red-400 bg-transparent hover:bg-red-400 hover:text-white transition-colors"
						on:click={() => { deleteMemory(editingMemoryFilename); showMemoryModal = false; }}
					>Удалить</button>
				{:else}
					<div></div>
				{/if}
				<div class="flex gap-2">
					<button
						class="px-4 py-2 rounded-md text-sm font-medium text-gray-400 bg-transparent hover:text-gray-200 transition-colors"
						on:click={() => (showMemoryModal = false)}
					>Отмена</button>
					<button
						class="px-4 py-2 rounded-md text-sm font-medium text-white bg-blue-500 hover:bg-blue-600 transition-colors"
						on:click={saveMemory}
					>Сохранить</button>
				</div>
			</div>
		</div>
	</div>
{/if}
