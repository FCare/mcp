/**
 * Joshua Mic Input Processor - AudioWorklet pour la capture audio
 * Adapté de SOCWebClient pour Joshua avec chunks de 80ms à 24kHz
 */
class JoshuaMicProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        
        // Configuration des chunks pour Joshua
        this.TARGET_SAMPLE_RATE = 24000;
        this.CHUNK_DURATION = 0.08; // 80ms
        this.SAMPLES_PER_CHUNK = this.TARGET_SAMPLE_RATE * this.CHUNK_DURATION; // 1920 samples
        
        // Buffer pour accumuler les samples
        this.pcmBuffer = [];
        
        // État d'enregistrement
        this.isRecording = false;
        
        // Écouter les messages du main thread
        this.port.onmessage = (event) => {
            if (event.data.command === 'start') {
                this.isRecording = true;
                this.pcmBuffer = []; // Reset buffer
                console.log('🎙️ Joshua Mic: Recording started');
            } else if (event.data.command === 'stop') {
                this.isRecording = false;
                console.log('🎙️ Joshua Mic: Recording stopped');
            }
        };
        
        console.log('🎙️ Joshua Mic Processor initialized');
    }
    
    /**
     * Resample audio data vers 24kHz
     */
    resampleTo24kHz(inputData, sourceSampleRate) {
        if (sourceSampleRate === this.TARGET_SAMPLE_RATE) {
            return Array.from(inputData);
        }
        
        const ratio = sourceSampleRate / this.TARGET_SAMPLE_RATE;
        const outputLength = Math.floor(inputData.length / ratio);
        const output = new Array(outputLength);
        
        for (let i = 0; i < outputLength; i++) {
            const srcIndex = Math.floor(i * ratio);
            output[i] = inputData[srcIndex] || 0;
        }
        
        return output;
    }
    
    /**
     * Process audio data - appelée automatiquement par le navigateur
     */
    process(inputs, outputs, parameters) {
        // Si pas d'enregistrement, ne rien faire
        if (!this.isRecording) {
            return true;
        }
        
        const input = inputs[0];
        if (!input || input.length === 0) {
            return true;
        }
        
        // Récupérer le canal mono (canal 0)
        const inputChannel = input[0];
        if (!inputChannel || inputChannel.length === 0) {
            return true;
        }
        
        // Resample vers 24kHz si nécessaire
        const resampledData = this.resampleTo24kHz(inputChannel, sampleRate);
        
        // Ajouter au buffer d'accumulation
        this.pcmBuffer.push(...resampledData);
        
        // Envoyer des chunks quand on a assez de samples
        while (this.pcmBuffer.length >= this.SAMPLES_PER_CHUNK) {
            // Extraire exactement 1920 samples (80ms à 24kHz)
            const chunk = this.pcmBuffer.splice(0, this.SAMPLES_PER_CHUNK);
            
            // Envoyer le chunk au main thread
            this.port.postMessage({
                type: 'audioChunk',
                data: chunk,
                sampleRate: this.TARGET_SAMPLE_RATE,
                duration: this.CHUNK_DURATION,
                samples: chunk.length
            });
        }
        
        // Continuer le processing
        return true;
    }
}

// Enregistrer le processor
registerProcessor('joshua-mic-processor', JoshuaMicProcessor);