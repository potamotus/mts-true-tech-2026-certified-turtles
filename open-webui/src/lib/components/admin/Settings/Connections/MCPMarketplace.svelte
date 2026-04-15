<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { getMcpRegistryServers } from '$lib/apis/configs';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n = getContext('i18n');

	export let installedUrls: Set<string> = new Set();
	export let onInstall: (connection: any) => void = () => {};

	let searchQuery = '';
	let searchTimeout: ReturnType<typeof setTimeout>;
	let servers: any[] = [];
	let nextCursor = '';
	let loading = false;
	let loadingMore = false;

	// Track which server is showing the API key input
	let connectingServer: string | null = null;
	let apiKeyInput = '';

	const fetchServers = async (cursor = '', append = false) => {
		if (append) {
			loadingMore = true;
		} else {
			loading = true;
			servers = [];
			nextCursor = '';
		}

		try {
			const res = await getMcpRegistryServers(localStorage.token, searchQuery, cursor);
			if (res) {
				if (append) {
					servers = [...servers, ...res.servers];
				} else {
					servers = res.servers;
				}
				nextCursor = res.metadata?.nextCursor || '';
			}
		} catch (e) {
			console.error('Failed to fetch MCP registry:', e);
		} finally {
			loading = false;
			loadingMore = false;
		}
	};

	const handleSearch = () => {
		clearTimeout(searchTimeout);
		searchTimeout = setTimeout(() => {
			fetchServers();
		}, 300);
	};

	const getRemote = (server: any) => {
		const remotes = server.server?.remotes || [];
		return remotes[0] || null;
	};

	const hasRequiredHeaders = (remote: any) => {
		return remote?.headers && remote.headers.length > 0;
	};

	const handleConnect = (entry: any) => {
		const server = entry.server;
		const remote = getRemote(entry);
		if (!remote) return;

		if (hasRequiredHeaders(remote)) {
			connectingServer = server.name;
			apiKeyInput = '';
		} else {
			doInstall(server, remote, '');
		}
	};

	const confirmConnect = (entry: any) => {
		const server = entry.server;
		const remote = getRemote(entry);
		if (!remote) return;
		doInstall(server, remote, apiKeyInput);
		connectingServer = null;
		apiKeyInput = '';
	};

	const doInstall = (server: any, remote: any, key: string) => {
		const hasHeaders = hasRequiredHeaders(remote);
		onInstall({
			type: 'mcp',
			url: remote.url,
			path: '',
			auth_type: hasHeaders ? 'bearer' : 'none',
			key: key,
			config: {
				enable: true,
				access_grants: [],
				function_name_filter_list: ''
			},
			info: {
				id: server.name,
				name: server.title || server.name,
				description: server.description || ''
			}
		});
	};

	const isInstalled = (entry: any) => {
		const remote = getRemote(entry);
		return remote ? installedUrls.has(remote.url) : false;
	};

	onMount(() => {
		fetchServers();
	});
</script>

<div class="space-y-3">
	<input
		class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-none"
		type="text"
		bind:value={searchQuery}
		on:input={handleSearch}
		placeholder={$i18n.t('Search MCP servers...')}
	/>

	{#if loading}
		<div class="flex justify-center py-8">
			<Spinner className="size-5" />
		</div>
	{:else if servers.length === 0}
		<div class="text-center text-xs text-gray-400 dark:text-gray-500 py-4">
			{$i18n.t('No servers found')}
		</div>
	{:else}
		<div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
			{#each servers as entry (entry.server.name + ':' + entry.server.version)}
				{@const server = entry.server}
				{@const remote = getRemote(entry)}
				{@const installed = isInstalled(entry)}
				<div
					class="flex flex-col justify-between rounded-lg border border-gray-100 dark:border-gray-850 p-3 text-sm"
				>
					<div>
						<div class="font-medium text-sm truncate">{server.title || server.name}</div>
						<div class="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">
							{server.description || ''}
						</div>
						{#if remote}
							<div class="text-xs text-gray-400 dark:text-gray-500 mt-1 truncate">
								{remote.url}
							</div>
						{/if}
					</div>

					<div class="mt-2.5">
						{#if installed}
							<button
								class="w-full px-3 py-1.5 text-xs rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 cursor-default"
								disabled
							>
								{$i18n.t('Connected')}
							</button>
						{:else if connectingServer === server.name}
							<div class="flex gap-1.5">
								<input
									class="flex-1 rounded-lg py-1.5 px-2.5 text-xs bg-gray-50 dark:bg-gray-850 dark:text-gray-300 outline-none"
									type="password"
									bind:value={apiKeyInput}
									placeholder={$i18n.t('API Key')}
									on:keydown={(e) => {
										if (e.key === 'Enter') confirmConnect(entry);
										if (e.key === 'Escape') {
											connectingServer = null;
											apiKeyInput = '';
										}
									}}
								/>
								<button
									class="px-3 py-1.5 text-xs font-medium rounded-lg bg-black text-white dark:bg-white dark:text-black hover:opacity-80 transition"
									on:click={() => confirmConnect(entry)}
									type="button"
								>
									{$i18n.t('Add')}
								</button>
							</div>
						{:else}
							<button
								class="w-full px-3 py-1.5 text-xs font-medium rounded-lg bg-black text-white dark:bg-white dark:text-black hover:opacity-80 transition"
								on:click={() => handleConnect(entry)}
								type="button"
							>
								{$i18n.t('Connect')}
							</button>
						{/if}
					</div>
				</div>
			{/each}
		</div>

		{#if nextCursor}
			<div class="flex justify-center pt-1">
				<button
					class="px-4 py-1.5 text-xs font-medium text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 transition"
					on:click={() => fetchServers(nextCursor, true)}
					disabled={loadingMore}
					type="button"
				>
					{#if loadingMore}
						<Spinner className="size-4" />
					{:else}
						{$i18n.t('Load more')}
					{/if}
				</button>
			</div>
		{/if}
	{/if}
</div>
