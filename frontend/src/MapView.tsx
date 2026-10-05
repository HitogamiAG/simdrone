import { Suspense, useEffect, useMemo, useRef } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Line, OrbitControls, OrthographicCamera, PerspectiveCamera, useGLTF } from '@react-three/drei'
import { CanvasTexture, EventDispatcher, Matrix4, MOUSE, Plane, Quaternion, Vector3 } from 'three'
import { worldGlbUrl } from './mapAssets'

export type MapManifest = {
  package_id: string
  version: string
  world_name: string
  coordinate_system: string
  units: string
  model: string
  glb_to_gazebo: number[][]
  control_points: { name: string; gazebo: number[]; glb: number[] }[]
}

type Pose = { position?: { x: number; y: number; z: number }; orientation?: { x: number; y: number; z: number; w: number } }
type Pad = { id: string; name: string; availability: string; assigned_drone_id: string | null; surface_pose: Pose; spawn_pose: Pose }
type Drone = { id: string; name: string; status?: string; spawn_pad_id?: string; simulation?: { pose?: Pose } }
type RoutePoint = { x: number; y: number; z: number }

function MissionRoute({ points, editable, onMovePoint }: { points: RoutePoint[]; editable: boolean; onMovePoint: (index: number, x: number, y: number) => void }) {
  if (!points.length) return null
  const vectors = points.map((point) => new Vector3(point.x, point.y, point.z))
  return <group>
    {vectors.length > 1 && <Line points={vectors} color="#4dabf7" lineWidth={2} />}
    {vectors.map((point, index) => <group key={index} position={point} onPointerDown={(event) => { if (editable) event.stopPropagation() }} onPointerMove={(event) => {
      if (!editable || !(event.nativeEvent.buttons & 1)) return
      const hit = event.ray.intersectPlane(new Plane(new Vector3(0, 0, 1), 0), new Vector3())
      if (hit) { event.stopPropagation(); onMovePoint(index, Number(hit.x.toFixed(2)), Number(hit.y.toFixed(2))) }
    }}>
      <mesh><sphereGeometry args={[0.18, 12, 8]} /><meshBasicMaterial color="#4dabf7" /></mesh>
      <SpriteLabel title={`${index + 1}`} subtitle={`Z ${points[index].z.toFixed(1)} м`} color="#4dabf7" position={[0, 0, 0.32]} scale={[1.25, 0.34, 1]} />
    </group>)}
  </group>
}


function WorldModel({ manifest }: { manifest: MapManifest }) {
  const { scene } = useGLTF(worldGlbUrl)
  const transform = useMemo(() => {
    const values = manifest.glb_to_gazebo.flat()
    if (values.length !== 16 || values.some((value) => !Number.isFinite(value))) throw new Error('Некорректная матрица GLB → Gazebo')
    return new Matrix4().set(...values as [number, number, number, number, number, number, number, number, number, number, number, number, number, number, number, number])
  }, [manifest])
  return <group matrix={transform} matrixAutoUpdate={false}><primitive object={scene} /></group>
}

function SpawnPad({ pad, onSelect }: { pad: Pad; onSelect: (pad: Pad) => void }) {
  const position = pad.surface_pose.position
  const spawnOrientation = pad.spawn_pose.orientation
  const rotation = useMemo(() => new Quaternion(spawnOrientation?.x ?? 0, spawnOrientation?.y ?? 0, spawnOrientation?.z ?? 0, spawnOrientation?.w ?? 1), [spawnOrientation?.x, spawnOrientation?.y, spawnOrientation?.z, spawnOrientation?.w])
  if (!position) return null
  const color = pad.availability === 'available' ? '#40c057' : pad.availability === 'occupied' ? '#fab005' : '#fa5252'
  const owner = pad.assigned_drone_id ? ` · ${pad.assigned_drone_id.slice(0, 8)}` : ''
  return <group position={[position.x, position.y, position.z + 0.025]} quaternion={rotation} onClick={(event) => { event.stopPropagation(); onSelect(pad) }}>
    <mesh>
      <ringGeometry args={[1.5, 1.62, 48]} /><meshBasicMaterial color={color} transparent opacity={0.85} depthWrite={false} />
    </mesh>
    <mesh>
      <circleGeometry args={[1.5, 48]} /><meshBasicMaterial transparent opacity={0} depthWrite={false} />
    </mesh>
    <mesh position={[0.42, 0, 0.045]} rotation={[0, 0, Math.PI / 2]}><cylinderGeometry args={[0.025, 0.025, 0.62, 8]} /><meshBasicMaterial color={color} /></mesh>
    <mesh position={[0.78, 0, 0.045]} rotation={[0, 0, -Math.PI / 2]}><coneGeometry args={[0.09, 0.22, 8]} /><meshBasicMaterial color={color} /></mesh>
    <SpriteLabel title={pad.name} subtitle={`${pad.availability === 'available' ? 'Свободна' : pad.availability === 'occupied' ? 'Занята' : 'Недоступна'}${owner}`} color={color} position={[0, 2.05, 0.2]} scale={[3.2, 0.62, 1]} />
  </group>
}

function DroneMarker({ drone, selected, onSelect, onContext }: { drone: Drone; selected: boolean; onSelect: (drone: Drone) => void; onContext: (drone: Drone, x: number, y: number) => void }) {
  const position = drone.simulation?.pose?.position
  const orientation = drone.simulation?.pose?.orientation
  if (!position) return null
  const rotation = new Quaternion(orientation?.x ?? 0, orientation?.y ?? 0, orientation?.z ?? 0, orientation?.w ?? 1)
  return <group position={[position.x, position.y, position.z]} quaternion={rotation}
    onClick={(event) => { event.stopPropagation(); onSelect(drone) }}
    onContextMenu={(event) => { event.stopPropagation(); event.nativeEvent.preventDefault(); onContext(drone, event.nativeEvent.clientX, event.nativeEvent.clientY) }}>
    <mesh rotation={[0, 0, -Math.PI / 2]}>
      <coneGeometry args={[0.22, 0.72, 4]} /><meshStandardMaterial color={selected ? '#4dabf7' : '#dee2e6'} />
    </mesh>
    {selected && <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0, -position.z + 0.01]}>
      <ringGeometry args={[0.42, 0.48, 32]} /><meshBasicMaterial color="#4dabf7" />
    </mesh>}
    <SpriteLabel title={drone.name} subtitle={drone.status ?? 'Состояние неизвестно'} color={selected ? '#4dabf7' : '#adb5bd'} position={[0, 0, 0.62]} scale={[2.5, 0.55, 1]} />
  </group>
}


function SpriteLabel({ title, subtitle, color, position, scale }: { title: string; subtitle: string; color: string; position: [number, number, number]; scale: [number, number, number] }) {
  const texture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = 512; canvas.height = 112
    const context = canvas.getContext('2d')
    if (context) {
      context.fillStyle = 'rgba(31,33,37,0.96)'; context.strokeStyle = color; context.lineWidth = 5
      context.beginPath(); context.roundRect(3, 3, 506, 106, 16); context.fill(); context.stroke()
      context.textAlign = 'center'; context.fillStyle = '#f1f3f5'; context.font = 'bold 32px sans-serif'; context.fillText(title, 256, 45, 480)
      context.fillStyle = '#adb5bd'; context.font = '26px sans-serif'; context.fillText(subtitle, 256, 82, 480)
    }
    return new CanvasTexture(canvas)
  }, [title, subtitle, color])
  useEffect(() => () => texture.dispose(), [texture])
  return <sprite position={position} scale={scale} renderOrder={1000}>
    <spriteMaterial map={texture} transparent depthTest={false} />
  </sprite>
}


function CameraBehavior({ view, zoom, focus, focusKey, follow }: { view: '2d' | '3d'; zoom: number; focus: [number, number, number]; focusKey: string; follow: boolean }) {
  const { camera, controls } = useThree()
  const orbit = controls as (EventDispatcher & { target: Vector3; update: () => void }) | null
  const focusRef = useRef(focus)
  useEffect(() => { focusRef.current = focus }, [focus])
  useEffect(() => {
    const [x, y, z] = focusRef.current
    // oxlint-disable-next-line react/immutability
    if (view === '2d') camera.zoom = 42 * zoom
    else camera.position.set(x + 14 / zoom, y - 18 / zoom, z + 14 / zoom)
    camera.updateProjectionMatrix()
    if (orbit) { orbit.target.set(x, y, z); orbit.update() }
  }, [camera, focusKey, orbit, view, zoom])
  const target = useMemo(() => new Vector3(...focus), [focus])
  useFrame((_, delta) => {
    if (!follow || !orbit) return
    orbit.target.lerp(target, 1 - Math.exp(-delta * 5))
    orbit.update()
  })
  return null
}

function Scene({ manifest, pads, drones, route, routeEditing, selectedDrone, view, zoom, focus, focusKey, follow, showPads, showDrones, showGrid, onSelectDrone, onSelectPad, onDroneContext, onAddRoutePoint, onMoveRoutePoint }: {
  manifest: MapManifest; pads: Pad[]; drones: Drone[]; route: RoutePoint[]; routeEditing: boolean; selectedDrone: string | null; view: '2d' | '3d'; zoom: number; focus: [number, number, number]; focusKey: string; follow: boolean; showPads: boolean; showDrones: boolean; showGrid: boolean
  onSelectDrone: (drone: Drone) => void; onSelectPad: (pad: Pad) => void; onDroneContext: (drone: Drone, x: number, y: number) => void; onAddRoutePoint: (point: RoutePoint) => void; onMoveRoutePoint: (index: number, x: number, y: number) => void
}) {
  const orthographic = view === '2d'
  return <>
    {view === '2d'
      ? <OrthographicCamera makeDefault position={[0, 0, 90]} up={[0, 0, 1]} zoom={42} near={0.1} far={1000} />
      : <PerspectiveCamera makeDefault position={[14, -18, 14]} up={[0, 0, 1]} fov={42} near={0.1} far={1000} />}
    <color attach="background" args={['#181a1e']} />
    <CameraBehavior view={view} zoom={zoom} focus={focus} focusKey={focusKey} follow={follow} />
    <ambientLight intensity={1.15} /><directionalLight position={[20, -20, 35]} intensity={2.1} />
    <Suspense fallback={null}><WorldModel manifest={manifest} /></Suspense>
    {routeEditing && <mesh position={[0, 0, 0.006]} onClick={(event) => { event.stopPropagation(); onAddRoutePoint({ x: Number(event.point.x.toFixed(2)), y: Number(event.point.y.toFixed(2)), z: 10 }) }}><planeGeometry args={[200, 200]} /><meshBasicMaterial transparent opacity={0} depthWrite={false} /></mesh>}
    {showGrid && <gridHelper args={[200, 40, '#454b55', '#30343b']} rotation={[Math.PI / 2, 0, 0]} position={[0, 0, 0.004]} />}
    {showPads && pads.map((pad) => <SpawnPad key={pad.id} pad={pad} onSelect={onSelectPad} />)}
    <MissionRoute points={route} editable={routeEditing} onMovePoint={onMoveRoutePoint} />
    {showDrones && drones.map((drone) => <DroneMarker key={drone.id} drone={drone} selected={drone.id === selectedDrone} onSelect={onSelectDrone} onContext={onDroneContext} />)}
    <OrbitControls makeDefault enableDamping={false} enablePan enableZoom enableRotate={!orthographic && !routeEditing} mouseButtons={{ MIDDLE: MOUSE.PAN, RIGHT: MOUSE.PAN }} minPolarAngle={orthographic ? 0.001 : 0} maxPolarAngle={orthographic ? 0.001 : Math.PI} />
  </>
}

export default function MapView(props: {
  view: '2d' | '3d'; zoom: number; focus: [number, number, number]; focusKey: string; follow: boolean; manifest: MapManifest; pads: Pad[]; drones: Drone[]; route: RoutePoint[]; routeEditing: boolean; selectedDrone: string | null; showPads: boolean; showDrones: boolean; showGrid: boolean
  onSelectDrone: (drone: Drone) => void; onSelectPad: (pad: Pad) => void; onDroneContext: (drone: Drone, x: number, y: number) => void; onPointerMissed: () => void; onAddRoutePoint: (point: RoutePoint) => void; onMoveRoutePoint: (index: number, x: number, y: number) => void
}) {
  return <Canvas className="scene-root" onPointerMissed={props.onPointerMissed}>
    <Scene {...props} />
  </Canvas>
}
