import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({root:new URL('.',import.meta.url).pathname.replace(/^\/(\w:)/,'$1'),plugins:[react()],server:{host:'127.0.0.1',port:8946,strictPort:true,fs:{allow:['..']}},build:{outDir:'dist',emptyOutDir:true}});
