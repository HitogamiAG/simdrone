import { copyFile, mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'

const source = resolve(process.env.SIMDRONE_MODELS_ROOT ?? '../models/empty')
const target = resolve('public/models/empty')
await mkdir(target, { recursive: true })
await Promise.all(['world.glb', 'manifest.json'].map((name) => copyFile(resolve(source, name), resolve(target, name))))
