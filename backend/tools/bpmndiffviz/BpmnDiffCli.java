import java.io.File;
import java.util.Collections;

import org.camunda.bpm.model.bpmn.Bpmn;
import org.camunda.bpm.model.bpmn.BpmnModelInstance;

import ru.pais.vkr.comparator.entities.ComparisonResult;
import ru.pais.vkr.comparator.entities.CostConfig;
import ru.pais.vkr.services.model_comparators.ModelComparator;
import ru.pais.vkr.services.model_comparators.ants.AntsModelComparator;
import ru.pais.vkr.services.model_comparators.greedy.GreedyModelComparator;
import ru.pais.vkr.services.model_comparators.simulated_annealing.SimulatedAnnealingComparator;
import ru.pais.vkr.services.model_comparators.tabu_search.TabuSearchModelComparator;

/** Minimal command-line adapter around the BPMNDiffViz comparison core. */
public final class BpmnDiffCli {
    private BpmnDiffCli() {}

    public static void main(String[] args) {
        if (args.length != 3) {
            throw new IllegalArgumentException(
                "usage: BpmnDiffCli <algorithm> <source.bpmn> <target.bpmn>"
            );
        }

        ModelComparator comparator;
        switch (args[0].toLowerCase()) {
            case "greedy":
                comparator = GreedyModelComparator.INSTANCE;
                break;
            case "tabusearch":
                comparator = TabuSearchModelComparator.INSTANCE;
                break;
            case "ants":
                comparator = AntsModelComparator.INSTANCE;
                break;
            case "simulatedannealing":
                comparator = SimulatedAnnealingComparator.INSTANCE;
                break;
            default:
                throw new IllegalArgumentException("unknown algorithm: " + args[0]);
        }

        BpmnModelInstance source = Bpmn.readModelFromFile(new File(args[1]));
        BpmnModelInstance target = Bpmn.readModelFromFile(new File(args[2]));
        ComparisonResult result = comparator.compareModels(
            source, target, new CostConfig(), Collections.emptyMap(), false
        );
        System.out.println(result.getPenalty());
    }
}
