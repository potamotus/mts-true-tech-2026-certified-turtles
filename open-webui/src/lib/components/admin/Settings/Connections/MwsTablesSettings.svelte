<script lang="ts">
	import { onMount } from 'svelte';

	const apiBase = `${window.location.protocol}//${window.location.hostname}:8000`;

	let token = '';
	let statusText = '';
	let statusColor = '';
	let statusVisible = false;
	let statusTimeout: ReturnType<typeof setTimeout>;

	function showStatus(text: string, color: string, duration = 3000) {
		statusText = text;
		statusColor = color;
		statusVisible = true;
		if (statusTimeout) clearTimeout(statusTimeout);
		statusTimeout = setTimeout(() => { statusVisible = false; }, duration);
	}

	async function loadConfig() {
		try {
			const res = await fetch(`${apiBase}/api/v1/mws-tables/config`);
			const data = await res.json();
			if (data.MWS_TABLES_API_TOKEN) token = data.MWS_TABLES_API_TOKEN;
		} catch {}
	}

	async function verify() {
		const trimmed = token.trim();
		if (!trimmed) {
			showStatus('Введите токен', '#ef4444');
			return;
		}
		showStatus('Проверка...', '#9ca3af', 30000);
		try {
			await fetch(`${apiBase}/api/v1/mws-tables/config`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ MWS_TABLES_API_TOKEN: trimmed })
			});
			const res = await fetch(`${apiBase}/api/v1/mws-tables/verify`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ token: trimmed })
			});
			const data = await res.json();
			if (data.ok) {
				showStatus(`Подключено (${data.spaces ? data.spaces.length : 0} spaces)`, '#22c55e', 5000);
			} else {
				showStatus(data.error || 'Ошибка', '#ef4444', 5000);
			}
		} catch {
			showStatus('Ошибка сети', '#ef4444');
		}
	}

	onMount(loadConfig);
</script>

<div class="my-2">
	<div class="flex justify-between items-center text-sm">
		<div class="font-medium">MWS Tables</div>
		{#if statusVisible}
			<span class="text-xs font-medium" style="color:{statusColor}">{statusText}</span>
		{/if}
	</div>
	<div class="mt-1 text-xs text-gray-400 dark:text-gray-500">
		API-токен для подключения к MWS Tables.
	</div>
	<div class="mt-2.5">
		<div class="text-xs font-medium mb-1">API Token</div>
		<div class="flex gap-2">
			<input
				bind:value={token}
				class="w-full rounded-lg py-1.5 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
				type="password"
				placeholder="Введите токен MWS Tables"
			/>
			<button
				class="px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
				type="button"
				on:click={verify}
			>Verify</button>
		</div>
	</div>
</div>
